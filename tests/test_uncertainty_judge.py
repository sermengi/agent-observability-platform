from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

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
from obs_platform.evaluation.judges.uncertainty import (
    UncertaintyJudge,
    UncertaintyJudgeOutput,
)
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


async def test_uncertainty_judge_exposes_llm_based_metadata() -> None:
    judge = UncertaintyJudge(
        CapturingJudgeClient(
            {
                "passed": True,
                "score": 1.0,
                "reason": "appropriately hedged",
                "overconfident_claims": [],
            }
        )
    )

    assert judge.name == "uncertainty"
    assert judge.version == "1.0.0"
    assert judge.type is EvaluatorType.LLM_BASED


async def test_uncertainty_not_applicable_does_not_call_judge_client() -> None:
    client = CapturingJudgeClient(
        {
            "passed": False,
            "score": 0.0,
            "reason": "should not be used",
            "overconfident_claims": [],
        }
    )
    call_log: list[JudgeCallResult[Any]] = []

    result = await UncertaintyJudge(client).evaluate_async(
        _run(status="runtime_error", final_result_output=None),
        call_log,
    )

    assert result.passed is True
    assert result.label == "not_applicable"
    assert client.prompts == []
    assert call_log == []


async def test_uncertainty_prompt_uses_tool_results_and_excludes_llm_calls() -> None:
    client = CapturingJudgeClient(
        {
            "passed": True,
            "score": 0.3,
            "reason": "judge passed despite low informational score",
            "overconfident_claims": [],
        }
    )
    call_log: list[JudgeCallResult[Any]] = []

    result = await UncertaintyJudge(client).evaluate_async(
        _run(
            tool_result={"asset_id": "pump-1", "fault_code": "FE-002"},
            llm_output={"internal_claim": "do-not-include-this-llm-content"},
            final_result_output={
                "answer": "Pump 1 may have reduced flow efficiency."
            },
        ),
        call_log,
    )

    assert result.passed is True
    assert result.label == "pass"
    assert result.score == 0.3
    assert result.severity is None
    assert len(call_log) == 1
    prompt = client.prompts[0]
    assert "FE-002" in prompt
    assert "may have reduced flow efficiency" in prompt
    assert "do-not-include-this-llm-content" not in prompt


async def test_uncertainty_maps_overconfident_claims_to_findings() -> None:
    client = CapturingJudgeClient(
        {
            "passed": False,
            "score": 0.1,
            "reason": "one claim is overconfident",
            "overconfident_claims": [
                {
                    "claim_text": "The impeller is damaged.",
                    "evidence_gap": "No inspection or teardown was performed.",
                    "explanation": "Evidence only shows a flow-efficiency fault code.",
                }
            ],
        }
    )

    result = await UncertaintyJudge(client).evaluate_async(_run(), [])

    assert result.passed is False
    assert result.label == "fail"
    assert result.severity is None
    assert [finding.code for finding in result.findings] == [
        "overconfident_hypothesis"
    ]
    assert result.findings[0].message == "The impeller is damaged."
    assert result.findings[0].data == {
        "evidence_gap": "No inspection or teardown was performed.",
        "explanation": "Evidence only shows a flow-efficiency fault code.",
    }


async def test_overconfident_hypothesis_classifies_as_unsupported_claim() -> None:
    assert (
        FINDING_TO_FAILURE_TYPE["overconfident_hypothesis"]
        is FailureType.UNSUPPORTED_CLAIM
    )

    result = await UncertaintyJudge(
        CapturingJudgeClient(
            {
                "passed": False,
                "score": 0.0,
                "reason": "overconfident",
                "overconfident_claims": [
                    {
                        "claim_text": "The impeller is damaged.",
                        "evidence_gap": "Only a fault code is available.",
                        "explanation": "The answer states a hypothesis as fact.",
                    }
                ],
            }
        )
    ).evaluate_async(_run(), [])

    classification = FailureClassifier().classify(
        [
            EvaluatorOutcome(
                evaluator_name="uncertainty",
                evaluator_version="1.0.0",
                execution_status=EvaluatorExecutionStatus.COMPLETED,
                result=result,
            )
        ]
    )

    assert classification.primary_category is FailureType.UNSUPPORTED_CLAIM
    assert classification.max_severity == "error"


def test_uncertainty_output_schema_has_no_underconfidence_field() -> None:
    assert set(UncertaintyJudgeOutput.model_fields) == {
        "passed",
        "score",
        "reason",
        "overconfident_claims",
    }
    assert "underconfident_claims" not in UncertaintyJudgeOutput.model_fields


def test_uncertainty_output_rejects_extra_fields() -> None:
    try:
        UncertaintyJudgeOutput.model_validate(
            {
                "passed": True,
                "score": 1.0,
                "reason": "ok",
                "overconfident_claims": [],
                "underconfident_claims": [],
            }
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError")


def _run(
    *,
    status: str = "success",
    tool_result: dict[str, Any] | None = None,
    llm_output: dict[str, Any] | None = None,
    final_result_output: dict[str, Any] | None = None,
) -> EvaluationRunView:
    timestamp = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return EvaluationRunView(
        run_id="run-uncertainty",
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
        else {"answer": "The impeller is damaged."},
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
                else {"asset_id": "pump-1", "fault_code": "FE-002"},
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
