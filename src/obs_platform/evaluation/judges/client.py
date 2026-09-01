from __future__ import annotations

from abc import ABC, abstractmethod
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict

from obs_platform.config import JudgeSettings

STRUCTURED_OUTPUT_TOOL_NAME = "structured_judge_output"


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
        started_at = monotonic()
        raw_completion = await self._raw_complete(
            prompt=prompt,
            schema=response_model.model_json_schema(),
        )
        latency_ms = round((monotonic() - started_at) * 1000)

        result: JudgeCallResult[T] = JudgeCallResult(
            output=response_model.model_validate(raw_completion.output),
            model=self.model,
            provider=self.provider,
            latency_ms=latency_ms,
            prompt_tokens=raw_completion.prompt_tokens,
            completion_tokens=raw_completion.completion_tokens,
            estimated_cost_usd=raw_completion.estimated_cost_usd,
        )
        if call_log is not None:
            call_log.append(result)
        return result

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


def _extract_forced_tool_input(message: Any) -> dict[str, Any]:
    for block in message.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == STRUCTURED_OUTPUT_TOOL_NAME
        ):
            tool_input = block.input
            if not isinstance(tool_input, dict):
                raise TypeError("structured judge tool input must be an object")
            return tool_input

    raise ValueError("judge response did not include the forced structured tool call")


def _estimate_anthropic_cost_usd(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> float:
    input_rate, output_rate = _anthropic_price_per_million_tokens(model)
    return (
        (prompt_tokens / 1_000_000 * input_rate)
        + (completion_tokens / 1_000_000 * output_rate)
    )


def _anthropic_price_per_million_tokens(model: str) -> tuple[float, float]:
    if "sonnet" in model:
        return (3.0, 15.0)
    if "haiku" in model:
        return (0.8, 4.0)
    if "opus" in model:
        return (15.0, 75.0)
    return (0.0, 0.0)
