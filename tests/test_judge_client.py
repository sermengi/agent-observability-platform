from abc import ABC
from inspect import isabstract
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from obs_platform.config import JudgeSettings, Settings
from obs_platform.evaluation.judges.client import (
    AnthropicJudgeClient,
    JudgeCallResult,
    JudgeClient,
    JudgeOutputValidationError,
    RawJudgeCompletion,
    create_judge_client,
)


class ExampleJudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


class MockJudgeClient(JudgeClient):
    def __init__(self) -> None:
        super().__init__(provider="mock-provider", model="mock-model")
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    async def _raw_complete(
        self, prompt: str, schema: dict[str, Any]
    ) -> RawJudgeCompletion:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return RawJudgeCompletion(
            output={"verdict": "pass"},
            prompt_tokens=17,
            completion_tokens=5,
            estimated_cost_usd=0.00042,
        )


class SequencedJudgeClient(JudgeClient):
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        super().__init__(provider="mock-provider", model="mock-model")
        self.outputs = outputs
        self.prompts: list[str] = []

    async def _raw_complete(
        self, prompt: str, schema: dict[str, Any]
    ) -> RawJudgeCompletion:
        self.prompts.append(prompt)
        return RawJudgeCompletion(
            output=self.outputs[len(self.prompts) - 1],
            prompt_tokens=10 + len(self.prompts),
            completion_tokens=5,
            estimated_cost_usd=0.001,
        )


async def test_judge_client_base_owns_generate_structured_template_method() -> None:
    assert issubclass(JudgeClient, ABC)
    assert isabstract(JudgeClient)
    assert "generate_structured" in JudgeClient.__dict__
    assert "_raw_complete" in AnthropicJudgeClient.__dict__
    assert "generate_structured" not in AnthropicJudgeClient.__dict__


async def test_generate_structured_returns_output_and_call_metadata() -> None:
    client = MockJudgeClient()
    call_log: list[JudgeCallResult[Any]] = []

    result = await client.generate_structured(
        prompt="Judge this answer.",
        response_model=ExampleJudgeOutput,
        call_log=call_log,
    )

    assert result.output == ExampleJudgeOutput(verdict="pass")
    assert result.model == "mock-model"
    assert result.provider == "mock-provider"
    assert result.latency_ms >= 0
    assert result.prompt_tokens == 17
    assert result.completion_tokens == 5
    assert result.estimated_cost_usd == 0.00042
    assert call_log == [result]
    assert client.prompts == ["Judge this answer."]
    assert client.schemas == [ExampleJudgeOutput.model_json_schema()]


async def test_judge_settings_load_nested_environment_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB__HOST", "localhost")
    monkeypatch.setenv("DB__PORT", "5432")
    monkeypatch.setenv("DB__USER", "observability")
    monkeypatch.setenv("DB__PASSWORD", "local-password")
    monkeypatch.setenv("DB__NAME", "observability")
    monkeypatch.setenv("API__HOST", "127.0.0.1")
    monkeypatch.setenv("API__PORT", "9000")
    monkeypatch.setenv("API__LOG_LEVEL", "debug")
    monkeypatch.setenv("JUDGE__ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("JUDGE__MODEL", "claude-sonnet-4-6")

    settings = Settings(_env_file=None)

    assert isinstance(settings.judge, JudgeSettings)
    assert settings.judge.provider == "anthropic"
    assert settings.judge.anthropic_api_key == "test-key"
    assert settings.judge.model == "claude-sonnet-4-6"


async def test_create_judge_client_is_settings_driven_construction_point() -> None:
    settings = JudgeSettings(
        anthropic_api_key="test-key",
        model="claude-sonnet-4-6",
    )

    client = create_judge_client(settings)

    assert isinstance(client, JudgeClient)
    assert isinstance(client, AnthropicJudgeClient)


async def test_anthropic_client_uses_forced_tool_calling() -> None:
    class FakeUsage:
        input_tokens = 11
        output_tokens = 7

    class FakeToolUse:
        type = "tool_use"
        name = "structured_judge_output"
        input = {"verdict": "pass"}

    class FakeMessage:
        content = [FakeToolUse()]
        usage = FakeUsage()

    class FakeMessages:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] | None = None

        async def create(self, **kwargs: Any) -> FakeMessage:
            self.kwargs = kwargs
            return FakeMessage()

    class FakeAnthropicClient:
        def __init__(self) -> None:
            self.messages = FakeMessages()

    anthropic_client = FakeAnthropicClient()
    client = AnthropicJudgeClient(
        api_key="test-key",
        model="claude-sonnet-4-6",
        anthropic_client=anthropic_client,
    )

    result = await client.generate_structured(
        prompt="Return a verdict.",
        response_model=ExampleJudgeOutput,
    )

    request = anthropic_client.messages.kwargs
    assert request is not None
    assert request["model"] == "claude-sonnet-4-6"
    assert request["messages"] == [{"role": "user", "content": "Return a verdict."}]
    assert request["tools"] == [
        {
            "name": "structured_judge_output",
            "description": "Return the structured judge evaluation result.",
            "input_schema": ExampleJudgeOutput.model_json_schema(),
        }
    ]
    assert request["tool_choice"] == {
        "type": "tool",
        "name": "structured_judge_output",
    }
    assert result.output == ExampleJudgeOutput(verdict="pass")
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7


async def test_generate_structured_retries_invalid_schema_output_twice() -> None:
    client = SequencedJudgeClient(
        [
            {"verdict": "pass", "extra": "not allowed"},
            {"wrong_field": "pass"},
            {"verdict": "pass"},
        ]
    )
    call_log: list[JudgeCallResult[Any]] = []

    result = await client.generate_structured(
        prompt="Judge this answer.",
        response_model=ExampleJudgeOutput,
        call_log=call_log,
    )

    assert result.output == ExampleJudgeOutput(verdict="pass")
    assert len(call_log) == 3
    assert call_log[0].output == {"verdict": "pass", "extra": "not allowed"}
    assert call_log[1].output == {"wrong_field": "pass"}
    assert call_log[2] == result
    assert len(client.prompts) == 3
    assert client.prompts[0] == "Judge this answer."
    assert "Previous judge response failed validation" in client.prompts[1]
    assert "extra_forbidden" in client.prompts[1]
    assert "Field required" in client.prompts[2]


async def test_generate_structured_raises_after_retry_budget_exhausted() -> None:
    client = SequencedJudgeClient(
        [
            {"wrong_field": "one"},
            {"wrong_field": "two"},
            {"wrong_field": "three"},
        ]
    )
    call_log: list[JudgeCallResult[Any]] = []

    with pytest.raises(JudgeOutputValidationError):
        await client.generate_structured(
            prompt="Judge this answer.",
            response_model=ExampleJudgeOutput,
            call_log=call_log,
        )

    assert len(call_log) == 3
    assert len(client.prompts) == 3


async def test_generate_structured_does_not_retry_transport_error() -> None:
    class TransportErrorJudgeClient(JudgeClient):
        def __init__(self) -> None:
            super().__init__(provider="mock-provider", model="mock-model")
            self.attempts = 0

        async def _raw_complete(
            self, prompt: str, schema: dict[str, Any]
        ) -> RawJudgeCompletion:
            self.attempts += 1
            raise TimeoutError("provider timed out")

    client = TransportErrorJudgeClient()
    call_log: list[JudgeCallResult[Any]] = []

    with pytest.raises(TimeoutError):
        await client.generate_structured(
            prompt="Judge this answer.",
            response_model=ExampleJudgeOutput,
            call_log=call_log,
        )

    assert client.attempts == 1
    assert len(call_log) == 1
    assert call_log[0].output == {}
    assert call_log[0].prompt_tokens == 0
