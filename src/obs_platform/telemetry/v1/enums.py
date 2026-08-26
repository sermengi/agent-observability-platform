from enum import StrEnum


class RunStatus(StrEnum):
    SUCCESS = "success"
    TOOL_ERROR = "tool_error"
    RUNTIME_ERROR = "runtime_error"
    AWAITING_APPROVAL = "awaiting_approval"


class RunEventType(StrEnum):
    RUN_FINAL = "run_final"
    RUN_AWAITING_APPROVAL = "run_awaiting_approval"


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


class HITLState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class LLMCallType(StrEnum):
    INTERPRETATION = "interpretation"
    EVIDENCE_GATHERING = "evidence_gathering"
    SYNTHESIS = "synthesis"
