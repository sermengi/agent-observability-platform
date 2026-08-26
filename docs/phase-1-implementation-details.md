# Phase 1 (Telemetry Contract & Debug Fixtures) — Implementation Decisions

Captured from planning discussion, 2026-08-26. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 1 and building on the Phase 0 (Walking Skeleton) decisions without contradicting them. Decisions are locked task-by-task, following the Phase 1 task list from the implementation plan:

1. Implement versioned Pydantic models for `ExtendedRunEvent` and nested span/tool/LLM/HITL/usage/final-result records. **(locked, amended — see Task 1 amendment note under Task 3)**
2. Define stable `run_id`, `span_id`, `tool_call_id`, and `llm_call_id` requirements. **(locked)**
3. Define required versus optional fields and final/HITL snapshot lifecycle semantics. **(locked)**
4. Create a compact set of human-auditable RunEvent fixtures covering: healthy success, tool failure, trajectory error, retrieval failure, unsupported-claim candidate, policy violation, HITL pending, and HITL approved. **(locked)**
5. Include realistic mock LLM token/latency/cost fields even though no real LLM is used yet. **(locked)**
6. Add fixture-loading utilities and schema validation tests. **(locked)**
7. Document that RunEvent is a run-level telemetry snapshot rather than an event-sourcing event. **(locked)**

All seven Phase 1 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phase 0 was planned. See Success Criteria and Status at the bottom.

---

## Task 1 — Versioned Pydantic models for `ExtendedRunEvent` and nested records

### Versioning mechanism

- **Module-namespaced versioning**, not field-only. Models live under `src/obs_platform/telemetry/v1/`, with `ExtendedRunEvent.schema_version: Literal["1.0"]` mirroring the package path. A hypothetical future breaking change would live in a sibling `v2` package rather than mutating `v1` in place.
- Rationale: Phase 10 is an explicit later checkpoint reconciling this contract against Project 1's real emitter output. If that reconciliation ever reveals a genuine breaking-change need, module-namespaced versioning lets old fixtures/tests keep validating against the old shape while new code targets the new shape — a field-only scheme would have no way to let both coexist.
- Package layout: `enums.py` (all controlled-vocabulary `StrEnum`s), `models.py` (all contract models — kept as one file rather than one-per-entity, since the whole contract is ~8 tightly coupled types and splitting further would add import overhead without real modularity benefit), `fixture_loader.py` (Task 6), `fixtures/` (Task 4), and `__init__.py` re-exporting the public names.

### Unknown-field policy

- **`extra="ignore"`**, applied via a shared `TelemetryModel` base class (`model_config = ConfigDict(extra="ignore")`) that every contract model inherits from — one place to change the policy later, not one per class.
- Rationale: this is the transport contract between two independently-deployed services (Project 1 and Project 2). Lenient handling of unknown fields tolerates producer/consumer version skew across that deploy boundary — if Project 1's real emitter (Phase 10) ever gets ahead of Project 2 with a new field, ingestion degrades gracefully rather than hard-failing.
- Rejected alternative: `extra="forbid"` (the pattern already used on Project 1's own tool-facing schemas per its Phase 6 decisions). Rejected here because that precedent was about LLM-argument validation inside one process; this is a cross-service wire contract, where forward-compatible skew tolerance matters more than catching our own typos immediately.

### Controlled vocabularies: `StrEnum`, not `Literal`

- Every closed vocabulary (`RunStatus`, `RunEventType`, `ExecutionStatus`, `HITLState`, `LLMCallType`) is a `StrEnum` (Python 3.12), not a bare `Literal[...]` type alias.
- Rationale: these vocabularies get referenced by name from multiple future phases (Phase 2 status filtering, Phase 4 deterministic evaluators), not just validated once at the ingestion boundary. A real importable enum member (`RunStatus.SUCCESS`) is safer against typos at call sites than comparing raw string literals, at the modest cost of one extra class per vocabulary.
- **Shared vocabularies where entities mean the same thing**: one `ExecutionStatus` enum (`SUCCESS`, `FAILURE`, `ERROR`) is reused by `Span.status`, `ToolCall.status`, and `LLMCall.status`, rather than three separate enums that could drift. `RunStatus` stays a separate, richer enum for the run's own top-level lifecycle status (see Task 3).
- **One shared `ErrorInfo` model** (`category: str`, `code: str | None`, `message: str`, `failed_component: str | None`) is reused for `Span.error`, `ToolCall.error`, `LLMCall.error`, and the run-level `runtime_error` — one error shape, not four ad hoc ones.

### Model composition

Mirrors the design doc's §6 table directly:

- `ExtendedRunEvent`: `schema_version`, `event_type`, `run_id`, `agent_name`/`agent_version`/`prompt_version`/`environment`, `raw_input`, `normalized_input: str | None`, `scenario_id: str | None`, `started_at`, `completed_at: datetime | None`, `status`, `execution_latency_ms: int | None`, `wall_clock_duration_ms: int | None`, `resume_count: int = 0`, `spans: list[Span]`, `tool_calls: list[ToolCall]`, `llm_calls: list[LLMCall]`, `hitl: HITLInfo` (always present, see Task 3), `usage: UsageSummary`, `final_result: FinalResult | None`, `runtime_error: ErrorInfo | None`.
- `Span`: `span_id`, `parent_span_id: str | None`, `name: str` (free text, **not** an enum — Project 1's specific graph-step names shouldn't be baked into Project 2's agent-agnostic core contract), `sequence`, `started_at`, `completed_at: datetime | None`, `status: ExecutionStatus`, `input`/`output`/`metadata: dict[str, Any] | None` (selected, not full state), `error: ErrorInfo | None`.
- `ToolCall`: `tool_call_id`, `span_id`, `tool_name: str`, `sequence`, `arguments: dict[str, Any]`, `result: dict[str, Any] | None`, `started_at`, `completed_at`, `latency_ms: int | None`, `retry_count: int = 0`, `status: ExecutionStatus`, `error: ErrorInfo | None`.
- `LLMCall`: `llm_call_id`, `span_id`, `call_type: LLMCallType` (values pinned in Task 5), `model: str`, `provider: str`, `started_at`, `completed_at`, `latency_ms: int | None`, `prompt_tokens`/`completion_tokens`/`total_tokens: int | None`, `estimated_cost_usd: float | None`, `input_payload`/`output_payload: dict[str, Any] | None`, `status: ExecutionStatus`, `error: ErrorInfo | None`.
- `HITLInfo`: `required: bool`, `state: HITLState`, `checkpoint_id: str | None`, `decision: Literal["approve", "reject"] | None`, `requested_at: datetime | None`, `decided_at: datetime | None`, plus `pending_action: dict[str, Any] | None` — **amendment, see Task 3**.
- `UsageSummary`: `total_llm_calls`, `total_tool_calls`, `total_tokens: int`, `total_estimated_cost_usd: float`, `total_retries: int`.
- `FinalResult`: `output: dict[str, Any]` (opaque — Project 2 doesn't validate Project 1's structured-output schema, just carries it), `source_references: list[str] = []`.
- `estimated_cost_usd` is a plain `float`, not `Decimal` — this is observability telemetry, not a billing ledger, and the design doc itself calls it "estimated."
- All `datetime` fields are timezone-aware UTC; naive datetimes are rejected, not silently assumed.

### Test / Validation

- [ ] Every contract model lives under `obs_platform.telemetry.v1` and imports correctly from that path.
- [ ] `ExtendedRunEvent.schema_version` is a fixed `Literal["1.0"]`.
- [ ] An unrecognized field on any contract model is silently dropped (`extra="ignore"`), not rejected — confirmed by a test that supplies an unknown key and asserts successful validation with that key absent from the resulting instance.
- [ ] `Span.status`, `ToolCall.status`, and `LLMCall.status` all type-check against the same `ExecutionStatus` enum — confirmed by code inspection, not three separate enums.
- [ ] `Span.error`, `ToolCall.error`, `LLMCall.error`, and `ExtendedRunEvent.runtime_error` all type-check against the same `ErrorInfo` model.
- [ ] `Span.name` accepts arbitrary free text — no enum constraint — confirmed by a fixture using a name not present in any predefined vocabulary.

---

## Task 2 — Stable `run_id`, `span_id`, `tool_call_id`, `llm_call_id` requirements

### ID format

- **Opaque, minimally-constrained strings.** Each ID field is `str` with `Field(min_length=1, max_length=256)` plus a validator stripping whitespace and rejecting the empty-after-strip case. No regex or algorithm-specific format (UUID, ULID, etc.) is enforced.
- Rationale: Project 2 should not assume or dictate Project 1's internal ID-generation scheme. Keeping the contract opaque on format means whatever Project 1 actually emits (Phase 10) slots in without translation, and keeps the contract a pure shape-contract rather than leaking a specific producer implementation detail into the transport layer.
- Rejected alternative: a prescribed format (e.g. mandatory UUID4 for `run_id`, deterministic composite strings for child IDs). Rejected because it would force Project 1's real ID generation to conform to Project 2's mandated shape, or require a translation shim at Phase 10 — both violate the "Project 2 must never assume Project 1 internals" principle.
- All four ID fields are **required and producer-supplied**, with no server-side generation fallback if missing — a missing ID is a validation error, not a silently-generated substitute.

### Uniqueness scope

- `run_id` is globally unique (one per logical run, for its whole lifetime across all its snapshots).
- `span_id`, `tool_call_id`, `llm_call_id` are each only required to be unique **within their parent run**, not globally — directly per the design doc's own Phase 1 test bullet ("stable child IDs are present and unique within each run"). This is also what lets Phase 2 use a composite natural key (`run_id` + child ID) rather than a globally-unique column per child table.

### Referential integrity — enforced at the model level

`ExtendedRunEvent` carries an `@model_validator(mode="after")` (this becomes the single shared validator later extended by Task 3) that checks, per instance:

- Uniqueness of `span_id` within `spans`, `tool_call_id` within `tool_calls`, `llm_call_id` within `llm_calls`.
- Every non-null `span.parent_span_id` matches a `span_id` present in that same event's `spans`.
- Every `tool_call.span_id`/`llm_call.span_id` matches a `span_id` present in `spans`.

A violation raises a `ValueError`, surfaced by Pydantic as a `ValidationError` — this is the "representative missing/invalid fields fail with controlled validation errors" behavior the design doc's own Phase 1 test list asks for.

### Child-ID stability across snapshots

- **Child IDs (`span_id`/`tool_call_id`/`llm_call_id`) are stable across re-emissions of the same run.** A span/tool_call/llm_call that already existed in an earlier snapshot of a `run_id` must be re-emitted with the same ID in a later snapshot; only genuinely new activity (e.g. post-resume) gets new IDs.
- Rationale: this enables true per-row idempotent upsert in Phase 2 (matching incoming children against existing rows by `(run_id, child_id)`), and lets a reviewer diff two sequential snapshots of the same run and see exactly what changed — considered important enough to accept the added producer-side bookkeeping burden this places on Project 1's emitter (a separate project, changeable in its own later integration phase).
- Rejected alternative: treating each snapshot as independent, with Phase 2 doing a coarser "replace the full child set for this `run_id`" on each ingestion. Rejected because it forecloses fine-grained snapshot-to-snapshot diffing, which was judged valuable enough to justify the extra producer obligation.
- **Not enforceable by a single-snapshot Pydantic validator** — this is inherently a cross-request guarantee. It's documented here as a producer contract obligation, and tested in two places instead: Task 4's `hitl_pending`/`hitl_approved` fixture pair is hand-authored so carried-over items share IDs (proving the fixture corpus honors the rule), and Phase 2's actual ingestion upsert logic enforces it at runtime.

### Fixture ID convention

Fixtures use short, human-readable IDs (e.g. `run_id="run-gs08-001"`, `span_id="span-evidence-gathering"`) rather than random UUIDs — both keeps the corpus manually inspectable and demonstrates that Project 2 genuinely doesn't care about ID format.

### Test / Validation

- [ ] All four ID fields reject empty and whitespace-only strings.
- [ ] A `span`/`tool_call`/`llm_call` ID that duplicates another within the same list raises a `ValidationError`.
- [ ] A `span.parent_span_id` or a `tool_call.span_id`/`llm_call.span_id` referencing a `span_id` absent from `spans` raises a `ValidationError`.
- [ ] `run_id` is required; omitting it raises a `ValidationError`.
- [ ] The `hitl_pending`/`hitl_approved` fixture pair (Task 4) shares an identical `run_id`, and every span/tool_call/llm_call ID present in `hitl_pending` is also present in `hitl_approved` with the same ID.

---

## Task 3 — Required vs. optional fields and final/HITL snapshot lifecycle semantics

### `RunStatus` stays minimal and purely runtime

- `RunStatus = {SUCCESS, TOOL_ERROR, RUNTIME_ERROR, AWAITING_APPROVAL}` — four values, all describing the producer's own runtime health, never an evaluation-layer conclusion.
- Rationale: several of Task 4's fixture names (trajectory error, retrieval failure, unsupported-claim candidate, policy violation) describe conditions a **Phase 4/5 evaluator** discovers by inspecting a run's content after the fact — Project 1 itself has no way to self-report "I made a trajectory error." Keeping `RunStatus` scoped to runtime outcomes only directly honors the design doc's "runtime success must remain distinct from future behavioral evaluation success" constraint, and keeps the enum stable as Phase 4/5's own failure taxonomy grows independently.
- Rejected alternative: a broader `RunStatus` enumerating the fixture categories directly (`TRAJECTORY_ERROR`, `POLICY_VIOLATION`, etc.) for at-a-glance fixture readability. Rejected because it would require the producer to pre-judge conclusions that are Project 2's job to compute independently, re-coupling two layers the design doc explicitly separates.
- Consequence: the trajectory-error, retrieval-failure, unsupported-claim-candidate, and policy-violation fixtures (Task 4) all carry `status="success"` — their content, not their status field, is what makes them interesting.

### Lifecycle representation: a three-way split

Rather than overload `status`, three already-existing fields each own one orthogonal concern:

- **`event_type: RunEventType`** — terminal or not. Two values: `RUN_FINAL` and `RUN_AWAITING_APPROVAL`. No third "resumed" value — a resumed run's outcome is just another `RUN_FINAL` snapshot with `resume_count > 0`.
- **`status: RunStatus`** — the run's runtime health (above). Always `AWAITING_APPROVAL` during `RUN_AWAITING_APPROVAL`.
- **`hitl.state: HITLState`** — `{NOT_REQUIRED, PENDING, APPROVED, REJECTED}`, the actual approval sub-lifecycle. Both an approved and a rejected outcome read `status="success"` at the top level — mirroring Project 1's own Phase 6 decision that a human declining a draft is a normal, successful completion, not an error — keeping the two projects' semantics consistent rather than reinventing the framing here.

### Task 1 amendment — `HITLInfo.pending_action`

- Surfaced during this task's discussion: Task 1's `HITLInfo` had no field to carry *what's actually pending*. Added `pending_action: dict[str, Any] | None` — opaque, same treatment as `FinalResult.output` — populated only while `state == PENDING`.
- Also locked here: **`hitl` is always present on `ExtendedRunEvent`**, never `None`. A run with no HITL involvement still carries `HITLInfo(required=False, state=NOT_REQUIRED, pending_action=None, ...)`, removing a null-check burden from every downstream consumer — same uniformity reasoning as `usage` always being present even for a zero-token run.

### Required/optional matrix by lifecycle stage

| Field | `RUN_FINAL` (success) | `RUN_FINAL` (tool/runtime error) | `RUN_FINAL` (HITL approved/rejected) | `RUN_AWAITING_APPROVAL` |
|---|---|---|---|---|
| `completed_at` | required | required | required | must be `None` |
| `status` | `SUCCESS` | `TOOL_ERROR`/`RUNTIME_ERROR` | `SUCCESS` | `AWAITING_APPROVAL` |
| `final_result` | required | `None` | required (even a rejection produces a final structured answer) | `None` |
| `runtime_error` | `None` | required | `None` | `None` |
| `hitl.required` | `False` | `False` | `True` | `True` |
| `hitl.state` | `NOT_REQUIRED` | `NOT_REQUIRED` | `APPROVED`/`REJECTED` | `PENDING` |
| `hitl.pending_action` | `None` | `None` | `None` | required |

`normalized_input` and `scenario_id` stay optional across every stage.

### Enforcement

These rules extend the same `ExtendedRunEvent` model validator introduced in Task 2 — one place holding all cross-field consistency rules, not two that could drift.

### Test / Validation

- [ ] A `RUN_FINAL` event with `completed_at=None` raises a `ValidationError`; a `RUN_AWAITING_APPROVAL` event with `completed_at` set raises one too.
- [ ] `status="success"` with `final_result=None` raises a `ValidationError`; `status` in `{TOOL_ERROR, RUNTIME_ERROR}` with `runtime_error=None` raises one too.
- [ ] `hitl.state="pending"` with `pending_action=None` raises a `ValidationError`.
- [ ] `hitl` is present (never `None`) on every fixture, including `healthy_success`.
- [ ] The trajectory-error, retrieval-failure, unsupported-claim-candidate, and policy-violation fixtures all validate with `status="success"` — confirmed by inspection, proving these are content-level rather than status-level distinctions.

---

## Task 4 — The canonical fixture corpus

### Storage format

- **Static JSON files**, one per scenario, under `src/obs_platform/telemetry/v1/fixtures/`.
- Rationale: this is the most literal reading of "human-auditable" — what's committed to the repo is exactly the wire payload `POST /v1/runs` will eventually receive, with no Python indirection between the two, and it doubles as ready-made request bodies for manual testing once Phase 2 exists.
- Rejected alternative: Python factory functions returning validated model instances. Rejected mainly on directness grounds — factories would need an extra rendering step to produce the literal JSON that Phase 2's ingestion tests actually need, and this phase's own goal is a corpus that *is* the target real contract, not a generator for it.

### Vocabulary realism

- Fixtures reuse Project 1's real, already-documented vocabulary where known: tool names `resolve_asset`, `get_asset_status`, `get_maintenance_history`, `get_plant_policy`, `create_work_order_draft`, `submit_work_order`, `search_maintenance_docs`; asset `PUMP-103`; fault codes `FE-002`–`FE-004`; work order IDs `WO-001`–`WO-003`; policy IDs `PP-001`/`PP-002`.
- The HITL pair (`hitl_pending`/`hitl_approved`) reuses this real vocabulary directly, since it represents Project 1's actual documented GS-08 scenario. The other six fixtures use plausible but invented asset IDs (`PUMP-101`, `PUMP-204`), since they don't need to match a specific frozen Project 1 golden scenario — only be shape-realistic.

### The eight fixtures

| Fixture | Asset / scenario | `status` | `event_type` | Key content |
|---|---|---|---|---|
| `healthy_success` | `PUMP-101` | `SUCCESS` | `RUN_FINAL` | Clean baseline: resolve → status → history → synthesis. |
| `tool_failure` | `PUMP-204` | `TOOL_ERROR` | `RUN_FINAL` | `get_asset_status` fails fatally; `final_result=None`. Only fixture exercising the `TOOL_ERROR` path. |
| `trajectory_error` | `PUMP-101`, `scenario_id="GS-DEBUG-TRAJ-01"` (synthetic, not a real Project 1 GS number) | `SUCCESS` | `RUN_FINAL` | `create_work_order_draft` called without prior evidence gathering — an ordering violation for `TrajectoryEvaluator`. |
| `retrieval_failure` | `PUMP-204` | `SUCCESS` | `RUN_FINAL` | `search_maintenance_docs` succeeds but returns zero documents; agent proceeds with a hedged answer. Recovered/degraded, not fatal — the deliberate counterpart to `tool_failure`. |
| `unsupported_claim_candidate` | `PUMP-101` | `SUCCESS` | `RUN_FINAL` | Evidence is adequate, but `final_result.output` asserts a claim untraceable to it — a groundedness gap, distinct from `retrieval_failure`'s evidence-quantity gap. |
| `policy_violation` | `PUMP-204` | `SUCCESS` | `RUN_FINAL` | `submit_work_order` appears with no preceding draft/approval — a deliberately adversarial trace proving `PolicyEvaluator` catches a critical PP-002 violation independently of Project 1's own guard. |
| `hitl_pending` | `PUMP-103`, `scenario_id="GS-08"`, `run_id="run-gs08-001"` | `AWAITING_APPROVAL` | `RUN_AWAITING_APPROVAL` | Draft created via the full evidence chain including `get_plant_policy`; `hitl.pending_action` carries the high-priority draft (PP-001 recurrence floor applied). |
| `hitl_approved` | same `run_id="run-gs08-001"` | `SUCCESS` | `RUN_FINAL` | Carries over every ID from `hitl_pending` unchanged, adds a new `submit_work_order` call producing `WO-003`, `hitl.state="approved"`, `resume_count=1`. |

### Test / Validation

- [ ] All 8 fixture files parse as valid JSON and validate against `ExtendedRunEvent`.
- [ ] `tool_failure` is the only fixture with `status="tool_error"`.
- [ ] `hitl_pending` and `hitl_approved` share `run_id="run-gs08-001"` and all carried-over child IDs (cross-referenced with Task 2).
- [ ] `hitl_approved` contains at least one ID (the `submit_work_order` tool call) absent from `hitl_pending`.
- [ ] Each fixture's file-level comment/docstring (outside the JSON payload itself, e.g. in the loader manifest or a sibling note) states what it's designed to exercise — particularly `policy_violation`, flagged as an intentionally adversarial trace the real guarded agent shouldn't actually produce.

---

## Task 5 — Realistic mock LLM token/latency/cost fields

### Generation approach

- **A small deterministic authoring-time helper**, `scripts/generate_mock_llm_usage.py` — dev-only, not part of the shipped `obs_platform` package, not imported by the app, tests, or CI. Run manually, once, per fixture (or once over the whole set) while authoring; it reads a short representative text snippet from each `LLMCall`'s `input_payload`/`output_payload`, computes `prompt_tokens`/`completion_tokens`/`latency_ms`/`estimated_cost_usd`, and patches the fixture JSON in place. After that, the committed JSON is the frozen source of truth — the script is never re-run automatically.
- Rationale: guarantees internal consistency (cost genuinely derives from token counts × rate, rather than independently hand-picked numbers that might not add up) for a one-time authoring cost.
- Rejected alternative: fully hand-authored fixed values per call. Rejected because nothing would guarantee `estimated_cost_usd` actually matches token counts × rate, undercutting "realistic" if anyone checks the arithmetic.

### Mocked model identity

- `model="claude-sonnet-4-6"`, `provider="anthropic"` — the model Project 1 actually uses.

### `LLMCallType` values (deferred from Tasks 1/3, resolved here)

- `{INTERPRETATION, EVIDENCE_GATHERING, SYNTHESIS}` — mapping to Project 1's known graph structure. No `JUDGE` value; that belongs to this project's own Phase 6 LLM-as-judge evaluators, living on `EvaluationResult`, not on a producer-emitted `LLMCall`.

### Mock cost/latency formulas

- Token estimate: ~4 characters per token, applied to each call's representative snippet.
- Cost: `prompt_tokens × $0.000003 + completion_tokens × $0.000015` (a plausible Sonnet-tier 5:1 output:input pricing ratio; illustrative for telemetry realism, not a claim about current real Anthropic pricing).
- Latency: `300ms + completion_tokens × 8ms` — a fixed baseline plus a per-token generation cost, so longer `SYNTHESIS` calls read as slower than short `EVIDENCE_GATHERING` tool-selection calls without needing per-call-type special-casing.
- Rounding: `estimated_cost_usd` to 6 decimal places; token counts and `latency_ms` to the nearest integer.

### Test / Validation

- [ ] Every `LLMCall` in every fixture has `model="claude-sonnet-4-6"`, `provider="anthropic"`, and non-null `prompt_tokens`/`completion_tokens`/`latency_ms`/`estimated_cost_usd`.
- [ ] For every `LLMCall`, `estimated_cost_usd` equals `prompt_tokens × 0.000003 + completion_tokens × 0.000015` to within rounding tolerance — confirmed by a test recomputing the formula against each fixture's stored values.
- [ ] `call_type` on every `LLMCall` is one of `INTERPRETATION`/`EVIDENCE_GATHERING`/`SYNTHESIS`.
- [ ] `tool_failure` (the only fixture that never reaches synthesis) has no `SYNTHESIS`-type `LLMCall`.

---

## Task 6 — Fixture-loading utilities and schema validation tests

### Discovery mechanism

- **Explicit manifest** — `FIXTURE_MANIFEST: dict[str, str]` in `fixture_loader.py`, enumerating exactly the 8 canonical fixtures by name, rather than a directory glob.
- Rationale: keeps the canonical set a single reviewable fact in code, matching this phase's own framing of a fixed, deliberately curated corpus rather than an open-ended folder, and guards against a stray or work-in-progress JSON file silently being swept into "all fixtures pass validation"-style tests.
- Rejected alternative: directory-glob discovery (auto-derive fixture names from filenames). Rejected for the minor loss of a single explicit enumeration of the promised set, judged not worth the small duplication savings.

### Loader module

- `src/obs_platform/telemetry/v1/fixture_loader.py` — lives in the shipped package, not under `tests/`, because Phase 2's ingestion test suite needs to import these same fixtures directly as normal ingestion test payloads.
- `load_fixture(name: str) -> ExtendedRunEvent` and `load_all_fixtures() -> dict[str, ExtendedRunEvent]`, both re-exported from `v1/__init__.py`.
- `load_fixture` raises straight through: an unrecognized name raises `FixtureNotFoundError(KeyError)`; a fixture failing schema validation lets Pydantic's `ValidationError` propagate untouched. No silent `None`, no catch-and-log.
- **No caching** — fixtures are tiny, so re-reading costs nothing measurable, and caching a mutable Pydantic instance risks one test's mutation leaking into another's.

### Test layout

- `tests/telemetry/v1/test_fixture_loader.py`: `test_all_fixtures_validate` (parametrized over the manifest), `test_hitl_pending_and_approved_share_run_identity` (equal `run_id`, matching carried-over IDs, at least one genuinely new ID in `hitl_approved`), `test_child_ids_unique_per_run` (parametrized over all 8, belt-and-suspenders regression check on top of the Task 2 model validator).
- `tests/telemetry/v1/test_contract_validation.py`: deliberately-invalid cases built inline from a mutated `healthy_success` dict (not committed as extra JSON fixtures, to keep the canonical corpus limited to genuinely valid examples) — missing required field, invalid enum value, dangling `parent_span_id`, duplicate `tool_call_id`, inconsistent lifecycle combination — each parametrized with a descriptive id.

### Test / Validation

- [ ] `load_fixture("nonexistent")` raises `FixtureNotFoundError`.
- [ ] `load_fixture` returns a distinct instance on each call (no shared mutable cache) — confirmed by mutating one returned instance and reloading to check the mutation didn't persist.
- [ ] Every entry in `FIXTURE_MANIFEST` corresponds to an existing file under `fixtures/`.
- [ ] All test cases described above are implemented and passing.

---

## Task 7 — Document that `RunEvent` is a snapshot, not an event-sourcing event

### Placement and content

- Primary documentation lives as a **docstring on the `ExtendedRunEvent` class**, not a new standalone doc — surfaces automatically in the model's JSON-schema `description` (useful later in Phase 2's OpenAPI docs), and avoids creating a `docs/` folder Phase 0 explicitly deferred to Phase 11.
- A short reinforcing summary also goes in the `v1/__init__.py` module docstring, pointing to `ExtendedRunEvent`'s docstring for the full explanation.
- Content explains: each instance is a complete, self-contained restatement of a run's current state, not an incremental delta or an append-only event; a normal run produces exactly one `RUN_FINAL` snapshot; a HITL run produces an intermediate `RUN_AWAITING_APPROVAL` snapshot followed by a `RUN_FINAL` snapshot that fully restates the run (not a delta), reusing IDs for carried-over items; consumers should treat a new snapshot for a known `run_id` as authoritative and superseding, upserted in place, never appended.
- The docstring points to the `hitl_pending`/`hitl_approved` fixtures by name as the canonical worked example, rather than duplicating an inline example that could drift out of sync with them.

### Test / Validation

- [ ] `ExtendedRunEvent.__doc__` is non-empty and mentions both "snapshot" and that it is not an event-sourcing event — a simple presence/content check, since this is documentation rather than behavior.
- [ ] No new `docs/` directory or README changes exist as a result of this task — confirmed by review, consistent with Phase 0's explicit deferral of expanded documentation to Phase 11.

---

## Success Criteria

- [ ] `ExtendedRunEvent` and its nested models are fully specified, versioned under `obs_platform.telemetry.v1`, and enforce internal consistency (ID uniqueness, referential integrity, lifecycle-field consistency) through a single shared model validator — closing every "controlled validation error" test bullet the design doc asks for (Tasks 1–3).
- [ ] The eight canonical fixtures are real, inspectable JSON files that collectively exercise every `RunStatus`/`event_type`/`hitl.state` combination this phase defines, reuse Project 1's genuine vocabulary where it's known, and are honest about where content is synthetic or intentionally adversarial (Task 4).
- [ ] Every `LLMCall` carries internally-consistent, realistically-shaped token/latency/cost fields against the actual `claude-sonnet-4-6` model Project 1 uses, without requiring any real LLM invocation (Task 5).
- [ ] A reusable, fail-fast fixture loader lives in the shipped package (not test-only), ready for Phase 2's ingestion tests to import directly, backed by a validation test suite covering both the positive corpus and representative negative cases (Task 6).
- [ ] The snapshot-vs-event-sourcing distinction — the conceptual key to understanding why child IDs must be stable across HITL snapshots — is documented at the source, not left implicit (Task 7).
- [ ] Project 2 can now be developed entirely against this contract, with no live Maintenance Agent dependency, matching this phase's own stated goal.

## Status

All seven Phase 1 tasks are locked. Phase 1 planning is complete. Next: proceed to implementation, or move on to Phase 2 (Ingestion & Persistence) planning discussion.