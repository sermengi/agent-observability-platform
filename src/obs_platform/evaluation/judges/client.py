from __future__ import annotations

from abc import ABC, abstractmethod
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from obs_platform.config import JudgeSettings

STRUCTURED_OUTPUT_TOOL_NAME = "structured_judge_output"
MAX_STRUCTURED_OUTPUT_ATTEMPTS = 3


class JudgeOutputValidationError(ValueError):
    pass


class RawJudgeOutputError(ValueError):
    pass


class RawJudgeCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float


class JudgeCallResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: T
    model: str
    provider: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float


class JudgeClient(ABC):
    def __init__(self, *, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    async def generate_structured[T: BaseModel](
        self,
        prompt: str,
        response_model: type[T],
        call_log: list[JudgeCallResult[Any]] | None = None,
    ) -> JudgeCallResult[T]:
        current_prompt = prompt
        schema = response_model.model_json_schema()
        last_validation_error: str | None = None

        for attempt_index in range(MAX_STRUCTURED_OUTPUT_ATTEMPTS):
            started_at = monotonic()
            try:
                raw_completion = await self._raw_complete(
                    prompt=current_prompt,
                    schema=schema,
                )
            except RawJudgeOutputError as exc:
                last_validation_error = str(exc)
                _append_call_log(
                    call_log,
                    self._raw_failed_call(round((monotonic() - started_at) * 1000)),
                )
                if attempt_index == MAX_STRUCTURED_OUTPUT_ATTEMPTS - 1:
                    raise JudgeOutputValidationError(last_validation_error) from exc
                current_prompt = _validation_retry_prompt(
                    original_prompt=prompt,
                    invalid_response={},
                    validation_error=last_validation_error,
                )
                continue
            except Exception:
                _append_call_log(
                    call_log,
                    self._raw_failed_call(round((monotonic() - started_at) * 1000)),
                )
                raise

            latency_ms = round((monotonic() - started_at) * 1000)
            try:
                output = response_model.model_validate(raw_completion.output)
            except ValidationError as exc:
                last_validation_error = str(exc)
                _append_call_log(
                    call_log,
                    self._raw_attempt_call(raw_completion, latency_ms),
                )
                if attempt_index == MAX_STRUCTURED_OUTPUT_ATTEMPTS - 1:
                    raise JudgeOutputValidationError(last_validation_error) from exc
                current_prompt = _validation_retry_prompt(
                    original_prompt=prompt,
                    invalid_response=raw_completion.output,
                    validation_error=last_validation_error,
                )
                continue

            result: JudgeCallResult[T] = JudgeCallResult(
                output=output,
                model=self.model,
                provider=self.provider,
                latency_ms=latency_ms,
                prompt_tokens=raw_completion.prompt_tokens,
                completion_tokens=raw_completion.completion_tokens,
                estimated_cost_usd=raw_completion.estimated_cost_usd,
            )
            _append_call_log(call_log, result)
            return result

        raise JudgeOutputValidationError(last_validation_error or "invalid output")

    def _raw_attempt_call(
        self, raw_completion: RawJudgeCompletion, latency_ms: int
    ) -> JudgeCallResult[Any]:
        return JudgeCallResult[Any](
            output=raw_completion.output,
            model=self.model,
            provider=self.provider,
            latency_ms=latency_ms,
            prompt_tokens=raw_completion.prompt_tokens,
            completion_tokens=raw_completion.completion_tokens,
            estimated_cost_usd=raw_completion.estimated_cost_usd,
        )

    def _raw_failed_call(self, latency_ms: int) -> JudgeCallResult[Any]:
        return JudgeCallResult[Any](
            output={},
            model=self.model,
            provider=self.provider,
            latency_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=0,
            estimated_cost_usd=0.0,
        )

    @abstractmethod
    async def _raw_complete(
        self, prompt: str, schema: dict[str, Any]
    ) -> RawJudgeCompletion:
        raise NotImplementedError


class AnthropicJudgeClient(JudgeClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int = 1024,
        anthropic_client: Any | None = None,
    ) -> None:
        super().__init__(provider="anthropic", model=model)
        self.max_tokens = max_tokens
        if anthropic_client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise RuntimeError(
                    "Anthropic judge client requires the 'anthropic' package"
                ) from exc

            anthropic_client = AsyncAnthropic(api_key=api_key)
        self._client = anthropic_client

    async def _raw_complete(
        self, prompt: str, schema: dict[str, Any]
    ) -> RawJudgeCompletion:
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "name": STRUCTURED_OUTPUT_TOOL_NAME,
                    "description": "Return the structured judge evaluation result.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": STRUCTURED_OUTPUT_TOOL_NAME},
        )

        return RawJudgeCompletion(
            output=_extract_forced_tool_input(message),
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
            estimated_cost_usd=_estimate_anthropic_cost_usd(
                model=self.model,
                prompt_tokens=message.usage.input_tokens,
                completion_tokens=message.usage.output_tokens,
            ),
        )


def create_judge_client(settings: JudgeSettings) -> JudgeClient:
    if settings.provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise ValueError("anthropic judge provider requires an API key")
        return AnthropicJudgeClient(
            api_key=settings.anthropic_api_key,
            model=settings.model,
            max_tokens=settings.max_tokens,
        )


def _append_call_log(
    call_log: list[JudgeCallResult[Any]] | None,
    result: JudgeCallResult[Any],
) -> None:
    if call_log is not None:
        call_log.append(result)


def _validation_retry_prompt(
    *,
    original_prompt: str,
    invalid_response: dict[str, Any],
    validation_error: str,
) -> str:
    return (
        f"{original_prompt}\n\n"
        "Previous judge response failed validation. Return a corrected structured "
        "tool response that matches the schema exactly.\n"
        f"Invalid response: {invalid_response}\n"
        f"Validation error: {validation_error}"
    )


def _extract_forced_tool_input(message: Any) -> dict[str, Any]:
    for block in message.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == STRUCTURED_OUTPUT_TOOL_NAME
        ):
            tool_input = block.input
            if not isinstance(tool_input, dict):
                raise RawJudgeOutputError(
                    "structured judge tool input must be an object"
                )
            return tool_input

    raise RawJudgeOutputError(
        "judge response did not include the forced structured tool call"
    )


def _estimate_anthropic_cost_usd(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    input_rate, output_rate = _anthropic_price_per_million_tokens(model)
    return (prompt_tokens / 1_000_000 * input_rate) + (
        completion_tokens / 1_000_000 * output_rate
    )


def _anthropic_price_per_million_tokens(model: str) -> tuple[float, float]:
    if "sonnet" in model:
        return (3.0, 15.0)
    if "haiku" in model:
        return (0.8, 4.0)
    if "opus" in model:
        return (15.0, 75.0)
    return (0.0, 0.0)
