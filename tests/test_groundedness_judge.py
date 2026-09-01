from datetime import UTC, datetime
from typing import Any

from obs_platform.evaluation.classifier import (
    FINDING_TO_FAILURE_TYPE,
    EvaluatorOutcome,
    FailureClassifier,
)
from obs_platform.evaluation.judges.client import (
    JudgeCallResult,
    JudgeClient,
    RawJudgeCompletion,
)
from obs_platform.evaluation.judges.groundedness import GroundednessJudge
from obs_platform.evaluation.types import (
    EvaluationRunView,
    EvaluatorExecutionStatus,
    EvaluatorType,
    FailureType,
)


class CapturingJudgeClient(JudgeClient):
    def __init__(self, output: dict[str, Any]) -> None:
        super().__init__(provider="mock-provider", model="mock-model")
        self.output = output
        self.prompts: list[str] = []

    async def _raw_complete(
        self, prompt: str, schema: dict[str, Any]
    ) -> RawJudgeCompletion:
        self.prompts.append(prompt)
        return RawJudgeCompletion(
            output=self.output,
            prompt_tokens=10,
            completion_tokens=8,
            estimated_cost_usd=0.001,
        )


async def test_groundedness_judge_exposes_llm_based_metadata() -> None:
    judge = GroundednessJudge(
        CapturingJudgeClient(
            {
                "passed": True,
                "score": 1.0,
                "reason": "grounded",
                "unsupported_claims": [],
            }
        )
    )

    assert judge.name == "groundedness"
    assert judge.version == "1.0.0"
    assert judge.type is EvaluatorType.LLM_BASED


async def test_groundedness_not_applicable_does_not_call_judge_client() -> None:
    client = CapturingJudgeClient(
        {
            "passed": False,
            "score": 0.0,
            "reason": "should not be used",
            "unsupported_claims": [],
        }
    )
    call_log: list[JudgeCallResult[Any]] = []

    result = await GroundednessJudge(client).evaluate_async(
        _run(status="tool_error", final_result_output=None),
        call_log,
    )

    assert result.passed is True
    assert result.label == "not_applicable"
    assert client.prompts == []
    assert call_log == []


async def test_groundedness_prompt_uses_tool_results_and_excludes_llm_calls() -> None:
    client = CapturingJudgeClient(
        {
            "passed": True,
            "score": 0.2,
            "reason": "judge passed despite low informational score",
            "unsupported_claims": [],
        }
    )
    call_log: list[JudgeCallResult[Any]] = []

    result = await GroundednessJudge(client).evaluate_async(
        _run(
            tool_result={"asset_id": "pump-1", "temperature_c": 80},
            llm_output={"internal_claim": "do-not-include-this-llm-content"},
            final_result_output={"answer": "Pump 1 temperature is 80 C."},
        ),
        call_log,
    )

    assert result.passed is True
    assert result.label == "pass"
    assert result.score == 0.2
    assert result.severity is None
    assert len(call_log) == 1
    assert call_log[0].output.reason == "judge passed despite low informational score"
    prompt = client.prompts[0]
    assert "temperature_c" in prompt
    assert "Pump 1 temperature is 80 C." in prompt
    assert "do-not-include-this-llm-content" not in prompt


async def test_groundedness_maps_unsupported_claims_to_findings() -> None:
    client = CapturingJudgeClient(
        {
            "passed": False,
            "score": 0.15,
            "reason": "one claim is unsupported",
            "unsupported_claims": [
                {
                    "claim_text": "The seal was replaced yesterday.",
                    "explanation": "No tool result mentions a seal replacement.",
                },
                {
                    "claim_text": "The pump is safe to restart.",
                    "explanation": "Evidence only contains temperature data.",
                },
            ],
        }
    )

    result = await GroundednessJudge(client).evaluate_async(_run(), [])

    assert result.passed is False
    assert result.label == "fail"
    assert result.severity is None
    assert [finding.code for finding in result.findings] == [
        "unsupported_claim",
        "unsupported_claim",
    ]
    assert result.findings[0].message == "The seal was replaced yesterday."
    assert result.findings[0].data == {
        "explanation": "No tool result mentions a seal replacement."
    }


async def test_unsupported_claim_finding_classifies_as_unsupported_claim() -> None:
    assert FINDING_TO_FAILURE_TYPE["unsupported_claim"] is FailureType.UNSUPPORTED_CLAIM

    result = await GroundednessJudge(
        CapturingJudgeClient(
            {
                "passed": False,
                "score": 0.0,
                "reason": "unsupported",
                "unsupported_claims": [
                    {
                        "claim_text": "The seal was replaced yesterday.",
                        "explanation": "No evidence supports this.",
                    }
                ],
            }
        )
    ).evaluate_async(_run(), [])

    classification = FailureClassifier().classify(
        [
            EvaluatorOutcome(
                evaluator_name="groundedness",
                evaluator_version="1.0.0",
                execution_status=EvaluatorExecutionStatus.COMPLETED,
                result=result,
            )
        ]
    )

    assert classification.primary_category is FailureType.UNSUPPORTED_CLAIM
    assert classification.max_severity == "error"


def _run(
    *,
    status: str = "success",
    tool_result: dict[str, Any] | None = None,
    llm_output: dict[str, Any] | None = None,
    final_result_output: dict[str, Any] | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return EvaluationRunView(
        run_id="run-groundedness",
        schema_version="1.0.0",
        event_type="run_final",
        agent_name="maintenance-agent",
        agent_version="agent-v1",
        prompt_version="prompt-v1",
        environment="test",
        raw_input={"query": "status"},
        normalized_input="status",
        scenario_id=None,
        started_at=timestamp,
        completed_at=timestamp,
        status=status,
        execution_latency_ms=100,
        wall_clock_duration_ms=110,
        resume_count=0,
        hitl_required=False,
        hitl_state="not_required",
        hitl_checkpoint_id=None,
        hitl_decision=None,
        hitl_requested_at=None,
        hitl_decided_at=None,
        hitl_pending_action=None,
        usage_total_llm_calls=1,
        usage_total_tool_calls=1,
        usage_total_tokens=42,
        usage_total_retries=0,
        usage_total_estimated_cost_usd=0.01,
        final_result_output=final_result_output
        if final_result_output is not None
        else {"answer": "The seal was replaced yesterday."},
        final_result_source_references=[],
        runtime_error_category=None,
        runtime_error_code=None,
        runtime_error_message=None,
        runtime_error_failed_component=None,
        spans=[],
        tool_calls=[
            {
                "tool_call_id": "tool-1",
                "span_id": "span-1",
                "tool_name": "get_asset_status",
                "sequence": 1,
                "arguments": {"asset_id": "pump-1"},
                "result": tool_result
                if tool_result is not None
                else {"asset_id": "pump-1", "status": "hot"},
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 20,
                "retry_count": 0,
                "status": "success",
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
        llm_calls=[
            {
                "llm_call_id": "llm-1",
                "span_id": "span-1",
                "sequence": 2,
                "call_type": "synthesis",
                "model": "agent-model",
                "provider": "agent-provider",
                "started_at": timestamp,
                "completed_at": timestamp,
                "latency_ms": 50,
                "prompt_tokens": 20,
                "completion_tokens": 22,
                "total_tokens": 42,
                "estimated_cost_usd": 0.01,
                "input_payload": {"messages": []},
                "output_payload": llm_output or {"internal_claim": "ignored"},
                "status": "success",
                "error_category": None,
                "error_code": None,
                "error_message": None,
                "error_failed_component": None,
            }
        ],
    )
