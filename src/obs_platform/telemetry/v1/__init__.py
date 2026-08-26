"""Version 1 telemetry contract.

Run events in this package are authoritative run-level snapshots, not
event-sourcing events. See ExtendedRunEvent for the full snapshot lifecycle
semantics.
"""

from obs_platform.telemetry.v1.enums import (
    ExecutionStatus,
    HITLState,
    LLMCallType,
    RunEventType,
    RunStatus,
)
from obs_platform.telemetry.v1.fixture_loader import (
    FIXTURE_MANIFEST,
    FixtureNotFoundError,
    load_all_fixtures,
    load_fixture,
)
from obs_platform.telemetry.v1.models import (
    ErrorInfo,
    ExtendedRunEvent,
    FinalResult,
    HITLInfo,
    LLMCall,
    Span,
    ToolCall,
    UsageSummary,
)

__all__ = [
    "ErrorInfo",
    "ExecutionStatus",
    "ExtendedRunEvent",
    "FIXTURE_MANIFEST",
    "FinalResult",
    "FixtureNotFoundError",
    "HITLInfo",
    "HITLState",
    "LLMCall",
    "LLMCallType",
    "RunEventType",
    "RunStatus",
    "Span",
    "ToolCall",
    "UsageSummary",
    "load_all_fixtures",
    "load_fixture",
]
