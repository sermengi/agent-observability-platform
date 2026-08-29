# Phase 4 (Deterministic Evaluation) — Implementation Decisions

Captured from planning discussion, 2026-08-29. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 4 and building on the Phase 0 (Walking Skeleton), Phase 1 (Telemetry Contract & Debug Fixtures), Phase 2 (Ingestion & Persistence), and Phase 3 (Core Query API & Operational Analytics) decisions without contradicting any of them. Decisions are locked task-by-task, following the Phase 4 task list from the implementation plan:

1. Define a small evaluator interface/registry with name, version, type, and `evaluate(run) -> EvaluationResult` semantics. **(locked)**
2. Implement `ToolExecutionEvaluator`. **(locked)**
3. Implement `StructuredOutputEvaluator`. **(locked)**
4. Implement `TrajectoryEvaluator` supporting required tools, forbidden tools, ordering constraints, and terminal conditions for scenario-aware runs. **(locked)**
5. Implement `PolicyEvaluator` for generic invariants such as unknown-asset stop and HITL authorization; encode critical policy severity deterministically. **(locked)**
6. Implement `EvidenceEvaluator` for required structured/document evidence in golden/scenario-aware runs. **(locked)**
7. Create machine-readable scenario/evaluator fixtures derived from the frozen Project 1 golden contracts where needed. **(locked)**
8. Persist evaluator results using the canonical `EvaluationResult` representation. **(locked)**

All eight Phase 4 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0–3 were planned. See Success Criteria and Status at the bottom.

---

## Task 1 — Evaluator interface/registry

### Evaluator identity and metadata

- `name: str` (a stable machine slug, e.g. `"tool_execution"`, distinct from the Python class name) and `version: str` (e.g. `"1.0.0"`) are class attributes on each evaluator, matching `evaluation_results.evaluator_name`/`evaluator_version` directly.
- `type: EvaluatorType = {DETERMINISTIC, LLM_BASED}` is **evaluator class metadata only, never persisted** — `evaluation_results` (Phase 2 Task 1) has no `type` column. It exists purely so a future orchestrator (or Phase 6's registry) can filter by kind; nothing reads it yet.

### Input shape — a dedicated view object, not raw ORM entities

- `evaluate()` receives a framework-agnostic `EvaluationRunView` (a Pydantic object: flat `spans`/`tool_calls`/`llm_calls` lists, plus the run's own scalar fields), built once from ORM rows before any evaluator runs — mirroring the shape Phase 3 Task 2's `RunDetailResponse` already established, not a second reused-elsewhere shape.
- Rejected alternative: passing the SQLAlchemy `AgentRun` ORM object directly. Rejected because it couples every evaluator to the ORM/DB session (tests would need a real or in-memory DB fixture instead of a plain object literal), risks accidental lazy-load queries inside evaluator logic, and cuts against this project's repeated pattern of keeping layers (persistence vs. query vs. now evaluation) independently testable.

### Registry — an explicit static list

- `DETERMINISTIC_EVALUATORS: list[Evaluator]` declared once in a single `registry.py`.
- Rejected alternative: decorator-based auto-registration (`@register_evaluator` populating a module-level dict at import time). Rejected as import-order-dependent and a bit of hidden magic for a cold reader, against a minor ergonomics win — consistent with this project's repeated preference for explicit, minimal mechanisms over dynamic ones (rejected Postgres sequences, rejected id-mapping tables, no generic SQL builders).

### `evaluate()` signature — sync, not async

- `def evaluate(self, run: EvaluationRunView) -> EvaluationResult` — plain synchronous method, matching what these five evaluators actually do (pure logic, no I/O).
- Rejected alternative: `async def evaluate(...)` from day one, anticipating Phase 6's LLM-judge evaluators needing real network I/O. Rejected per this project's consistent "don't build ahead of need" posture (deferred `regression_runs`, no speculative indexing, no speculative dataset expansion) — accepted as a foreseeable, not hypothetical, interface change Phase 6 will need to make.

### `EvaluationResult` shape — outcome only, no execution-status field

- `EvaluationResult`: `passed: bool`, `score: float | None`, `label: str | None`, `severity: str | None`, `reason: str`, `findings: list[EvaluationFinding]`.
- **No execution-status field** (no `pending`/`running`/`completed`/`failed`/`skipped` on this object). Phase 5 Task 2 explicitly owns defining that lifecycle — a real orchestration concept meaningless in Phase 4's fully-synchronous, non-orchestrated context. Task 8's persistence step hardcodes `status="completed"` for anything it persists, since it only ever persists a result an evaluator successfully returned. A "doesn't apply to this run" case (e.g. `TrajectoryEvaluator` on a live run) is expressed via `label="not_applicable"`, not a different status — keeping the two lifecycle concepts (did-it-run vs. did-it-pass) genuinely independent rather than conflating them ahead of Phase 5's own taxonomy.
- `findings: list[EvaluationFinding]` (`code: str`, `message: str`, `data: dict`) — structured, not a single blob, per the phase's own "findings must be structured; reason text is secondary" constraint. `reason` stays a one-line human-readable summary.
- **Cross-cutting severity rule**: `severity` is `None` on every evaluator's result **except `PolicyEvaluator`** — the task list explicitly calls out "critical policy severity encoded deterministically" only for Task 5, implying severity assignment elsewhere is Phase 5's `FailureClassifier` job (its own "deterministic severity mapping"), not something each evaluator should guess ahead of that taxonomy existing.
- `evaluate()` does not defensively catch its own bugs — a genuine evaluator bug propagates as an exception, consistent with this project's "let it bubble, don't swallow" posture (Phase 2 Task 3's uncaught DB errors, Phase 3's no custom error envelopes). Catching this and mapping it to `INCOMPLETE`/`failed` is Phase 5's orchestrator's job.

### Test / Validation

- [ ] Every evaluator exposes `name`/`version`/`type` as class attributes; none of the three appears in the `evaluation_results` schema for `type` specifically.
- [ ] `evaluate()` accepts an `EvaluationRunView` instance constructible without a DB session or SQLAlchemy import — confirmed by a unit test building one from plain literals.
- [ ] `DETERMINISTIC_EVALUATORS` is a plain list in `registry.py`; no decorator-based registration mechanism exists in the codebase.
- [ ] `EvaluationResult` has no field representing execution status (`pending`/`running`/etc.) — confirmed by code inspection.
- [ ] `severity` is `None` on every `EvaluationResult` produced by `ToolExecutionEvaluator`, `StructuredOutputEvaluator`, `TrajectoryEvaluator`, `EvidenceEvaluator` — confirmed across each evaluator's fixture tests.
- [ ] `findings` is always a list of structured objects with `code`/`message`/`data`, never a bare string or dict blob.

---

## Task 2 — `ToolExecutionEvaluator`

- **Grain**: one `EvaluationResult` per run, over the full `tool_calls` list on the run's view object — not one result per call. Individual bad calls surface only inside `findings`.
- **Pass/fail rule**: `passed = True` iff every `tool_calls` row has `status == SUCCESS`. `FAILURE` and `ERROR` are both treated as "unsuccessful" — the same equivalence Phase 3 Task 4's `failure_rate = (FAILURE + ERROR) / total` already established — but each finding still records the exact status, so the open FAILURE/ERROR semantic distinction (Known-Issue ISSUE-001) isn't lost, just not load-bearing here.
- **Retries not checked for "boundedness."** No hardcoded retry ceiling is introduced — `retry_count` is surfaced in each finding purely for visibility, never itself a cause for failing. Rejected alternative: a hardcoded threshold (e.g. `retry_count > 3`) — rejected as an arbitrary implementation-time-invented constant not sourced from any Project 1 policy or golden scenario, the same category of guardrail-against-a-hypothetical this project has avoided elsewhere.
- **Zero-tool-calls edge case**: vacuously `passed = True`, `score = None`, `label = "pass"` — nothing to have failed.
- **`score`**: `success_count / total_calls` (`None` only in the zero-calls case).
- **`label`**: `"pass"` / `"fail"`.
- **`severity`**: always `None` (per Task 1's cross-cutting rule).
- **`findings`**: one per non-`SUCCESS` call — `code="tool_call_failed"` or `"tool_call_error"` (matching the row's actual status), `data={tool_call_id, tool_name, status, retry_count, error_category, error_message}`.
- **`reason`**: one-line summary, e.g. `"3/4 tool calls succeeded"`.

### Test / Validation

- [ ] A run where every `tool_calls` row is `SUCCESS` produces `passed=True`, `score=1.0`, `label="pass"`, empty `findings`.
- [ ] A run with a mix of `SUCCESS`/`FAILURE`/`ERROR` produces `passed=False`, `score` equal to the exact success ratio, and one finding per non-`SUCCESS` row with the correct `code`.
- [ ] A run with zero `tool_calls` produces `passed=True`, `score=None`, `label="pass"`.
- [ ] A high `retry_count` on an otherwise-`SUCCESS` call produces no finding and does not affect `passed`/`score` — confirmed by a fixture with an artificially high `retry_count`.
- [ ] `severity` is `None` regardless of outcome.

---

## Task 3 — `StructuredOutputEvaluator`

- **No redundant re-check of Phase 1's `final_result` presence/absence lifecycle matrix.** Ingestion's model validator already guarantees `final_result` is present/absent exactly per the `RunStatus`/`event_type`/`hitl.state` combination before a row can reach the DB — a violating stored row is structurally impossible. Re-checking it here would be pure redundancy with no live failure path, matching Phase 2 Task 4's identical reasoning for skipping an analogous defensive re-check.
- **No citation/referential-integrity checking of `source_references` against evidence content.** Rejected because `tool_calls.result`/`llm_calls.output_payload` are opaque JSONB by design (`FinalResult.output` itself is explicitly documented as "opaque — Project 2 doesn't validate Project 1's structured-output schema, just carries it," Phase 1 Task 1) — Project 2 has no agent-agnostic way to confirm a domain ID is buried inside an opaque blob without assuming a specific producer's shape.
- **Core check**: when `final_result` is expected (`status=SUCCESS`, plain success or HITL approved/rejected), `output` must be a non-empty dict (`output != {}`). This is real incremental value beyond ingestion, which only enforces presence, not non-triviality.
- **Not applicable** when `final_result` is legitimately `None` (`TOOL_ERROR`/`RUNTIME_ERROR`/`AWAITING_APPROVAL`): `passed=True`, `label="not_applicable"`.
- **Scope stays narrow** — no cross-check against `source_references`/tool-call activity (e.g. flagging a non-empty answer with zero citations despite successful evidence-gathering calls); that concern is left to `EvidenceEvaluator`/Phase 6's groundedness judge, keeping this evaluator's single responsibility (output shape only) clean against the design doc's own five-evaluator split.
- **`findings`**: `code="empty_output"` when it fails; none otherwise.
- **`score`**: `None` — binary structural check, no meaningful fraction.

### Test / Validation

- [ ] A `SUCCESS` run with a non-empty `final_result_output` produces `passed=True`, `label` unset/`"pass"`.
- [ ] A `SUCCESS` run with `final_result_output = {}` produces `passed=False`, one `"empty_output"` finding.
- [ ] A `TOOL_ERROR`/`RUNTIME_ERROR`/`AWAITING_APPROVAL` run produces `passed=True`, `label="not_applicable"`, empty `findings`.
- [ ] No test or code path inspects `final_result_output`'s internal keys/text content — confirmed by code inspection (only `dict`/emptiness checks appear).
- [ ] No test or code path inspects `source_references`'s relationship to tool-call content.

---

## Task 4 — `TrajectoryEvaluator`

### Shared `ScenarioContract` model

- One `ScenarioContract` Pydantic model, introduced here and extended (not duplicated) by Tasks 5/6, matching design doc §17.1's single machine-readable scenario contract representation: `scenario_id`, `required_tools: list[str]`, `forbidden_tools: list[str]`, `ordering_constraints: list[tuple[str, str]]` (before, after — pairwise), `terminal: TerminalCondition | None`, `required_evidence: list[str]` (added by Task 6), `expected_asset_identity: str | None` (accepted for documentation-completeness with Project 1's real golden scenarios, but **not acted upon** — see below).
- `TerminalCondition`: `expected_status`, `expected_event_type`, `expected_hitl_required`, `expected_hitl_state` — all optional, reusing the exact three-way lifecycle split Phase 1 Task 3 locked (`status`/`event_type`/`hitl.state`), rather than a new span-name or last-tool-call signal.

### Ordering — pairwise precedence, not exact/subsequence matching

- Rejected alternative: ordered-subsequence matching (an ordered list of required tools checked as a subsequence of the actual call sequence). Rejected in favor of independent pairwise rules — naturally tolerant of optional/interleaved steps, each rule simple to state and localize a failure against, directly matching "ordering constraints... where the contract allows optional steps."
- A pair `(A, "before", B)` is checked only when both tools actually appear in the run; compares `tool_calls.sequence` at each tool's first occurrence.

### Known scope gap: `expected_asset_identity` not implemented

- Checking whether the run resolved the *correct* specific asset would require inspecting `tool_calls.arguments`/`result` for `resolve_asset` — both opaque JSONB. Recorded here explicitly (in the same spirit as `known-issues.md`) as an accepted v1 limitation, not a silent omission.

### Mechanics

- Required tool: present anywhere in `tool_calls` by `tool_name`, regardless of `status` (a required-but-failed tool is `ToolExecutionEvaluator`'s concern).
- Forbidden tool: must not appear at all, regardless of `status` — an attempted-but-failed/guard-rejected forbidden call is still itself the violation.
- **Non-golden runs** (`scenario_id` null/unrecognized): `passed=True`, `label="not_applicable"`, no findings. All generic/live-run invariants live in `PolicyEvaluator` instead — matching design doc §11.2's table (Trajectory: full contract for golden, blank for live; Policy: yes for both).
- **`findings`**: `code` in `{"missing_required_tool", "forbidden_tool_used", "ordering_violation", "terminal_condition_mismatch"}`, each with the specific tool/field in `data`.
- **`score`**: satisfied-constraints / total-constraints-checked.
- **`severity`**: always `None`.
- **Contract lookup**: static `SCENARIO_CONTRACTS: dict[str, ScenarioContract]` keyed by `scenario_id`, sourced from Task 7's fixtures — same explicit-static-registry pattern as Task 1's evaluator list.

### Test / Validation

- [ ] A run whose `scenario_id` has no matching entry in `SCENARIO_CONTRACTS` produces `passed=True`, `label="not_applicable"`.
- [ ] A golden-scenario run missing a `required_tools` entry produces a `"missing_required_tool"` finding and `passed=False`.
- [ ] A golden-scenario run containing a `forbidden_tools` entry (even with `status=ERROR`) produces a `"forbidden_tool_used"` finding.
- [ ] An ordering pair violated (B's sequence < A's sequence when A must precede B) produces an `"ordering_violation"` finding; the same pair with only one of the two tools present produces no finding.
- [ ] A run whose `status`/`event_type`/`hitl_state` doesn't match the contract's `TerminalCondition` produces a `"terminal_condition_mismatch"` finding.
- [ ] No code path inspects `tool_calls.arguments`/`result` content for asset identity — confirmed by code inspection.

---

## Task 5 — `PolicyEvaluator`

- **Applies universally** — golden and live runs alike, no `ScenarioContract` lookup involved. Matches design doc §11.2 ("Policy / guardrail compliance: Yes / Yes").
- **HITL authorization**: if `submit_work_order` appears anywhere in `tool_calls`, `agent_runs.hitl_state` must be `APPROVED`. Fully schema-based (known canonical tool name + generic `hitl_state` column) — no opaque content involved. This is exactly what the `policy_violation` fixture (Phase 1 Task 4) was built to exercise. **Severity: `"critical"`** — matching the test bullet's own wording and PP-002's consequential-action framing.
- **Unknown-asset stop**: if a span named `"unknown_asset"` is present, no asset-specific tool (`get_asset_status`, `get_maintenance_history`, `create_work_order_draft`, `submit_work_order`) may have a later `sequence`. **Severity: `"major"`** — distinct from HITL's `"critical"`, as a workflow-correctness bug rather than an unapproved consequential action.
- **Accepted coupling risk**: the unknown-asset check hardcodes a specific Project-1 span-name string as its detection signal. Rejected alternative: a trajectory-shape heuristic (resolve_asset called, no asset-specific tool followed, run still reached a non-empty `SUCCESS` answer) with no span-name coupling — rejected as genuinely ambiguous (can't distinguish a real unknown-asset stop from a live run that simply never needed those tools), undermining the test bullet's own expectation of a clean, confident detection. Flagged for re-verification once Phase 10's real Project 1 integration confirms the actual emitted span name.
- **Scope stays to exactly these two named invariants** — no speculative additional policy rules beyond what the task's test bullets ask for.
- **Result-level `severity`** = the maximum across triggered findings (mirrors the same primary/max-severity pattern the design doc uses at the `RunFailure` level) — both checks firing in one run reports `"critical"`.
- **`score`**: `None` — boolean invariant checks; severity carries the "how bad."
- **`label`**: `"pass"` / `"fail"` (no `"not_applicable"` — always applicable).
- **`findings`**: `code` in `{"unauthorized_consequential_action", "unknown_asset_downstream_call"}`, each with its own `data` and its own severity.

### Test / Validation

- [ ] A run containing `submit_work_order` with `hitl_state != APPROVED` produces `passed=False`, a `"unauthorized_consequential_action"` finding, and result-level `severity="critical"` — exercised directly by the `policy_violation` fixture.
- [ ] A run containing `submit_work_order` with `hitl_state == APPROVED` produces `passed=True` for this check.
- [ ] A run containing a span named `"unknown_asset"` followed by any asset-specific tool call at a later `sequence` produces an `"unknown_asset_downstream_call"` finding and result-level `severity="major"` (or `"critical"` if the HITL check also fired).
- [ ] A run with both violations present reports `severity="critical"` (the max), with two distinct findings each retaining their own severity in `data`.
- [ ] This evaluator produces a non-`"not_applicable"` result for both golden and live-style fixtures — confirmed it never skips based on `scenario_id`.

---

## Task 6 — `EvidenceEvaluator`

- **`ScenarioContract` extended** with `required_evidence: list[str]` (introduced under Task 4's shared model).
- **Check**: every ID in `required_evidence` must appear in `agent_runs.final_result_source_references` — plain list-membership against a native, non-opaque `TEXT[]` column.
- Rejected alternative: falling back to a raw string search across opaque `tool_calls.result`/`llm_calls.output_payload` content when an ID isn't cited, to distinguish "never gathered" from "gathered but not cited." Rejected as a step further into opaque-content coupling than anything else accepted in this phase, with real false-positive risk from substring matching.
- **Non-golden runs**: `passed=True`, `label="not_applicable"` — identical pattern to `TrajectoryEvaluator`.
- **`findings`**: one `code="missing_required_evidence"` entry per absent ID, `data={evidence_id}`.
- **`score`**: matched / total required.
- **`severity`**: always `None`.
- **Known, accepted limitation**: this only catches evidence never cited — "gathered but not cited" and "never gathered" are indistinguishable here, same category as Task 3's citation-referential-integrity gap.

### Test / Validation

- [ ] A golden-scenario run whose `final_result_source_references` contains every ID in the contract's `required_evidence` produces `passed=True`, `score=1.0`.
- [ ] A golden-scenario run missing one required evidence ID produces `passed=False`, exactly one `"missing_required_evidence"` finding, and `score` reflecting the partial ratio.
- [ ] A non-golden run produces `passed=True`, `label="not_applicable"`.
- [ ] No code path inspects `tool_calls.result`/`llm_calls.output_payload` content — confirmed by code inspection (only `final_result_source_references` is read).

---

## Task 7 — Machine-readable scenario/evaluator fixtures

- **Storage**: static JSON files under `src/obs_platform/evaluation/scenario_contracts/`, one per scenario, loaded via an explicit manifest dict — mirrors Phase 1 Task 6's fixture-loader pattern exactly (static JSON + explicit manifest + fail-fast loader), rejecting directory-glob discovery for the same reasons already locked there.
- **Coverage, this phase**: one real `ScenarioContract` for **GS-08** (the only golden scenario with actual ingested telemetry, via the `hitl_pending`/`hitl_approved` fixture pair), and one synthetic debug contract for **`trajectory_error`** (`scenario_id="GS-DEBUG-TRAJ-01"`) to exercise `TrajectoryEvaluator`'s violation paths. `policy_violation` needs no contract at all, since `PolicyEvaluator` runs unconditionally regardless of `scenario_id`.
- **`trajectory_error.json` (Phase 1 debug fixture) was amended, not left frozen.** It originally exercised only a missing-required-tool path; a second `get_asset_status` tool call (sequence 2, after `create_work_order_draft`) was added so it also exercises `ordering_violation` per this task's own checklist. Confirmed no other test asserts the fixture's prior exact shape.
- **Coverage for GS-01–GS-07 explicitly deferred, not abandoned.** The `ScenarioContract` schema is fully generic — extending coverage later (most likely alongside Phase 10's real Project 1 integration, or sooner if the fixture corpus grows) is purely additive data (a new JSON file + one manifest entry), requiring no evaluator code changes. Authoring them now, with no ingested telemetry to validate them against, would be pure speculation with nothing to catch a mistake — the same "don't build ahead of need" reasoning applied throughout this project (deferred `regression_runs`, no speculative indexing).

### Test / Validation

- [ ] `SCENARIO_CONTRACTS` contains exactly two entries: `"GS-08"` and `"GS-DEBUG-TRAJ-01"`.
- [ ] The `GS-08` contract's `required_tools`/`required_evidence` match what the `hitl_pending`/`hitl_approved` fixtures actually contain — confirmed by running `TrajectoryEvaluator`/`EvidenceEvaluator` against those fixtures and asserting `passed=True`.
- [ ] The `GS-DEBUG-TRAJ-01` contract's `ordering_constraints` are violated by the `trajectory_error` fixture by construction — confirmed `TrajectoryEvaluator` produces `passed=False` with an `"ordering_violation"` finding against it.
- [ ] Adding a new scenario contract requires touching only the fixtures directory and manifest — no evaluator source file changes — confirmed by a test contract added purely for this check.

---

## Task 8 — Persist evaluator results using the canonical `EvaluationResult` representation

- **Function shape**: `persist_evaluation_result(session: AsyncSession, run_id: str, evaluator: Evaluator, result: EvaluationResult) -> EvaluationResultRecord` — a plain function taking an injected session, mirroring this project's established pattern (Phase 2's ingestion service, Project 1's repository functions) rather than a class/service object.
- **Insert-only, never upsert.** Phase 2 Task 1 already locked `evaluation_results` as an append/history grain (`UNIQUE(run_id, evaluator_name, evaluator_version, regression_run_id)`, explicitly not latest-only) specifically so repeated evaluation runs (Phase 7 regressions) accumulate history. This function performs a plain `INSERT` with no `ON CONFLICT` handling — a second evaluation of the same run/evaluator/version simply lands as a second row.
- **`status="completed"` hardcoded at persistence time** — since this function only ever persists a result an evaluator successfully returned, per Task 1's decision to keep execution-status entirely out of `EvaluationResult` itself.
- **`regression_run_id`**: always `None` for everything Phase 4 produces — no regression orchestration exists yet; Phase 2 made this column nullable with no FK specifically to allow this state.
- **Transaction scope: independent per-evaluator commits, not one atomic transaction across all evaluators run against a run.** Rejected alternative: wrapping all evaluators' inserts for one run in a single transaction (mirroring Phase 2 Task 3's "one atomic transaction per ingested run"). Rejected because it would directly contradict the "evaluator infrastructure failure must not be mislabeled as agent failure" constraint — one evaluator's bug or transient DB failure would discard four other evaluators' perfectly valid, already-computed results. Independent commits also anticipate Phase 5 Task 2's per-evaluator execution-status model, which only makes sense if evaluators can genuinely succeed or fail independently of each other. A run ending up with a partial set of `evaluation_results` rows (e.g. 4 of 5) is accepted — making that partial state visible/queryable is explicitly Phase 5's job, not this task's.

### Test / Validation

- [ ] Persisting two `EvaluationResult`s for the same `(run_id, evaluator_name, evaluator_version)` (simulating a re-evaluation) inserts two distinct rows, not an upsert — confirmed by row count.
- [ ] Every row persisted by this function has `status="completed"` and `regression_run_id=None`.
- [ ] A forced DB failure while persisting one evaluator's result does not roll back or otherwise affect rows already committed for other evaluators on the same run — confirmed by a test that persists three results, forces a failure on a fourth, and verifies the first three remain committed.
- [ ] `persist_evaluation_result` is callable and testable with a plain test `AsyncSession`, with no FastAPI app or HTTP layer involved.

---

## Success Criteria

- [ ] All five deterministic evaluators (`ToolExecutionEvaluator`, `StructuredOutputEvaluator`, `TrajectoryEvaluator`, `PolicyEvaluator`, `EvidenceEvaluator`) share one small interface (`name`/`version`/`type`, sync `evaluate(run: EvaluationRunView) -> EvaluationResult`) and one explicit static registry, with no LLM call anywhere in this phase (Task 1).
- [ ] Every evaluator's outcome semantics (`passed`/`score`/`label`/`findings`) are independent of its execution status — no evaluator sets its own `status`; Task 8's persistence layer is the sole place `status="completed"` is assigned (Tasks 1, 8).
- [ ] Trajectory/Evidence checks apply only to scenario-aware runs (`not_applicable` otherwise); Policy checks apply universally — matching design doc §11.2's golden-vs-live table exactly (Tasks 4, 5, 6).
- [ ] `PolicyEvaluator` is the only evaluator that assigns `severity`, deterministically distinguishing `"critical"` (unauthorized consequential action) from `"major"` (unknown-asset downstream call) — closing both of this phase's named test bullets (Task 5).
- [ ] Every case where a check would require inspecting opaque `tool_calls.result`/`llm_calls.output_payload`/`arguments` content (citation referential integrity, asset-identity matching, gathered-but-uncited evidence) was identified and explicitly deferred rather than solved by quietly assuming a specific producer's internal shape — recorded against Tasks 3, 4, and 6 respectively.
- [ ] A real, currently-exercisable golden scenario (GS-08) and one synthetic debug scenario have machine-readable `ScenarioContract`s; the schema is generic enough that extending coverage to the remaining seven frozen Project 1 golden scenarios later requires no evaluator code changes (Task 7).
- [ ] `evaluation_results` accumulates one row per evaluator invocation (never overwritten), with each evaluator's persistence independent of the others' success or failure (Task 8).

## Status

All eight Phase 4 tasks are locked. Phase 4 planning is complete. Next: proceed to implementation, or move on to Phase 5 (Failure Classification & Evaluation API) planning discussion.