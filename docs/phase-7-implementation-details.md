# Phase 7 (Regression Evaluation) — Implementation Decisions

Captured from planning discussion, 2026-09-03 (in progress). These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 7 and building on the Phase 0–6 decisions without contradicting any of them. Decisions are locked task-by-task, following the Phase 7 task list from the implementation plan:

1. Implement `regression_runs` persistence and add optional `regression_run_id`/`scenario_id`/`repetition_index` linkage to `AgentRun`. **(locked)**
2. Define `RegressionRun` metadata: agent_version, provider/model, prompt_version, scenario contract version, evaluator versions, repetitions, status, timestamps, and optional baseline marker. **(locked)**
3. Implement a small regression runner/orchestrator that executes scenario → agent target → ingestion → evaluation. **(locked, amended by Task 8)**
4. First validate orchestration with a mocked agent using 2 scenarios × 2 repetitions. **(locked)**
5. Support controlled GS-08 HITL evaluation by exercising the real approval/resume API contract rather than bypassing the gate. **(locked)**
6. Implement final 8-scenario suite configuration with five repetitions per scenario. **(locked)**
7. Implement regression aggregation: overall evaluation pass rate, per-scenario pass rate, per-evaluator pass rate, failure distribution, average/p95 latency, tokens, agent cost, and evaluation cost. **(locked)**
8. Implement `POST /v1/regressions` and `GET /v1/regressions/{id}`; add `GET /v1/regressions` if inexpensive/useful. **(locked)**
9. Implement `GET /v1/analytics/scenarios`. **(locked)**
10. Warn or mark runs as not directly comparable when evaluator/scenario contract versions differ. **(locked)**

All ten Phase 7 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0–6 were planned. See Success Criteria and Status at the bottom.

---

## Task 1 — `regression_runs` persistence + `AgentRun` linkage

### Schema

- **`regression_runs`**: new table, surrogate `INTEGER` PK (`id`, auto-increment) — forced by the type already committed on `evaluation_results.regression_run_id` (Phase 2 Task 1's reserved bare column). Full metadata columns are Task 2's job; this task just establishes the table as a valid FK target plus whatever minimal scaffolding (`status`, timestamps) Task 2 fleshes out.
- **`evaluation_results.regression_run_id`**: upgraded via additive migration from bare/unconstrained `INTEGER` to a real `FOREIGN KEY → regression_runs.id`.
- **`agent_runs.regression_run_id`**: new, nullable `INTEGER FK → regression_runs.id`.
- **`agent_runs.repetition_index`**: new, nullable `INTEGER`, 0-indexed.
- **`agent_runs.scenario_id`**: no schema change — the column already exists (Phase 2 Task 1). What changes is *who writes it* for a regression-driven run (see below).
- FK delete behavior: no `ON DELETE CASCADE` from `regression_runs` — deleting a regression run must never silently cascade-delete the `agent_runs`/`evaluation_results` history under it. Default `RESTRICT`.
- Index: single-column index on `agent_runs.regression_run_id`, serving the known future "list runs for regression X" query (`GET /v1/regressions/{id}`).

### `scenario_id` authority — orchestrator-owned, not producer-trusted (locked)

- The regression runner is authoritative for `scenario_id`, `regression_run_id`, and `repetition_index` on any run it drives — it dispatched the scenario, so it doesn't need to trust the agent target's own `RunEvent` to echo the ID back correctly. This keeps all three regression-linkage fields under one consistent rule rather than treating `scenario_id` specially just because it happens to already have a producer-supplied path for live traffic.
- **Mechanism**: two-step, not a single extended write. Ingestion (`POST /v1/runs` or the internal ingestion service function) runs completely unchanged — same path a real Project 1 integration will use later. The regression orchestrator then calls a new, separate `persist_regression_linkage(session, run_id, regression_run_id, scenario_id, repetition_index)` function immediately after, which sets those three columns on the just-ingested `agent_runs` row. This mirrors the project's established pattern of layering independent persistence steps after core ingestion (`persist_evaluation_result`, `persist_run_failure`, `persist_judge_call`) rather than growing the ingestion function's own signature with regression-specific parameters.
- **Precedence**: the orchestrator's value always wins outright — an unconditional overwrite, no validation/reconciliation against whatever `scenario_id` the agent target's `RunEvent` happened to report. A mismatch would only ever indicate a bug in the mocked test harness itself (a real Project 1 agent has no obligation to know golden-scenario IDs at all), so there's nothing meaningful to reconcile.

### Guardrail: uniqueness constraint

- `UNIQUE(regression_run_id, scenario_id, repetition_index)` on `agent_runs` — a cheap DB-level belt-and-suspenders check against an orchestrator bug double-writing the same scenario/repetition slot within one regression run. Postgres treats multiple `NULL`s in a UNIQUE constraint as distinct by default, so ordinary live (non-regression) runs — where all three columns are `NULL` — are completely unaffected.

### Rejected alternative

- Producer-supplied `scenario_id` with ingestion left fully untouched (Option A). Rejected because it would leave `scenario_id` following a different trust rule than `regression_run_id`/`repetition_index` on the very same row, and it quietly assumes the real Project 1 agent will someday know about golden-scenario IDs — a Project 2/benchmark concept it has no inherent reason to own.

### Test / Validation

- [ ] `regression_runs` exists with an `id` PK; `evaluation_results.regression_run_id` carries a real FK to it (migration adds the constraint without altering the column's existing data/type).
- [ ] `agent_runs.regression_run_id`/`repetition_index` are nullable and `NULL` for every existing (non-regression) run.
- [ ] Ingesting a `RunEvent` whose own `scenario_id` differs from the regression runner's dispatched scenario results in the orchestrator's value winning on the persisted `agent_runs` row.
- [ ] Two `persist_regression_linkage` calls attempting the same `(regression_run_id, scenario_id, repetition_index)` triple raise a constraint violation; two ordinary live runs (all three `NULL`) never conflict with each other.
- [ ] "All runs for regression X" lookup uses the new index, not a sequential scan.

---

## Task 2 — `RegressionRun` metadata

### Schema

`regression_runs`:
- `id` — surrogate `INTEGER` PK.
- `name` — nullable `TEXT`, human label, no uniqueness constraint.
- `agent_model_provider` / `agent_model_name` — `TEXT`, `agent_`-prefixed to disambiguate from the judge's own `provider`/`model` fields (Phase 6).
- `prompt_version` — `TEXT`.
- `scenario_contract_version` — single `TEXT` column, a global suite-level version (e.g. `SCENARIO_CONTRACTS_VERSION`, bumped whenever any contract file in the set changes) rather than a per-scenario dict.
- `evaluator_versions` — `JSONB`, `{evaluator_name: version}` snapshot of the full 7-evaluator registry (5 deterministic + 2 LLM-based), captured at creation time, before execution starts. Design doc §17.3 orders "create RegressionRun with frozen metadata" as the first step of the execution flow — this settles the snapshot-vs-derive-later question upstream.
- `repetitions` — `INTEGER`, uniform per-scenario repetition count for this run (5 for the final suite, 2 for the mocked validation run).
- `scenario_ids` — `JSONB`/`TEXT[]`, the explicit list of scenarios included (not just a count) — needed to reconstruct/audit exactly which scenarios ran, and required for Task 4's 2-scenario subset.
- `status` — `TEXT` + `CHECK IN ('pending','running','completed','failed')` — tracks orchestration progress only, independent of agent behavioral pass/fail (mirrors Phase 5 Task 2's execution-status/outcome split). No `partial` state; per-scenario failures surface inside aggregation (Task 7), not here.
- `is_baseline` — `BOOLEAN DEFAULT FALSE`, with a partial unique index `WHERE is_baseline = true` (at most one designated baseline at a time).
- `started_at` / `completed_at` — nullable `TIMESTAMP`.

### `ScenarioContract` amendment required by this task

- Phase 4 Task 7's `ScenarioContract` model has no version concept at all. This task adds one **at the suite level, not the model level** — a single `SCENARIO_CONTRACTS_VERSION` constant alongside the existing manifest, not a new field on the `ScenarioContract` Pydantic model itself. No change to the individual contract JSON files.

### Consequence for Task 1's `regression_runs` scaffolding

- Task 1 only needed `id` to exist as an FK target; this task supplies every other column via a single combined migration (no separate migration needed for Task 1 vs Task 2 — `regression_runs` reaches its full locked shape in one `CREATE TABLE`).

### Rejected alternative

- Per-scenario `evaluator_versions`-style JSONB for scenario contract versioning (`{scenario_id: version}`, with a `version` field added to the `ScenarioContract` model itself). Rejected because it contradicts the design doc's singular `scenario_contract_version` field naming and the "8 scenarios as one frozen suite" framing used throughout this phase's task list; a global version is sufficient for Task 10's yes/no comparability warning, which doesn't need to know exactly which scenario changed.

### Test / Validation

- [ ] A `RegressionRun` created before any child run executes already has non-null `evaluator_versions` (all 7 entries) and `scenario_contract_version` populated — confirmed created independently of any `agent_runs`/`evaluation_results` existing yet.
- [ ] `regression_runs.status` rejects an out-of-vocabulary value via its `CHECK` constraint.
- [ ] Setting `is_baseline=true` on a second row while one already has it set either fails the partial unique index or requires an explicit unset-then-set — confirmed only one row can have `is_baseline=true` at a time.
- [ ] `scenario_ids` on a mocked 2-scenario validation run contains exactly those 2 IDs, not 8.

---

## Task 3 — Regression runner/orchestrator

### `ScenarioContract` extended with invocation input

- Adds `scenario_input: dict` to the existing `ScenarioContract` model (alongside `scenario_id`, `required_tools`, `forbidden_tools`, `ordering_constraints`, `terminal`, `required_evidence`, `expected_asset_identity`) — the actual request payload the orchestrator sends to the agent target to trigger the scenario. Additive extension of the one shared model, continuing the pattern Phase 4 Tasks 4–6 already established rather than introducing a parallel invocation-input structure.

### Orchestrator talks to Project 2 in-process, not over HTTP (locked)

- The runner imports and calls the same underlying service functions the API endpoints themselves call — `ingest_run_event(session, event)`, `run_evaluation(session, run_id)` — directly, with no HTTP client, no ASGI test server, no event-loop-over-a-loop ceremony.
- Rationale: the design doc's "call through the public API, not internals" instruction is specifically about *Project 1* (don't reach into its LangGraph internals) — it says nothing about how Project 2's own runner should invoke Project 2's own services. Established precedent inside this project points the other way for that case: every persistence/service function so far (`persist_evaluation_result`, `persist_run_failure`, `persist_judge_call`) has been deliberately kept callable and testable independent of FastAPI. Matches the plan's own "small orchestrator" framing.
- Consequence, noted for later: this in-process runner would need rework to drive a *remote* Project 2 deployment. Explicitly acceptable — out of scope for v1, consistent with "Project 1 real integration is still deferred."

### `AgentTarget` abstraction

- `AgentTarget(ABC)` with an abstract `async def run_scenario(contract: ScenarioContract) -> RunEvent`, mirroring `JudgeClient`'s template-method shape from Phase 6. One concrete implementation for now, `MockedAgentTarget`; anticipates a future `Project1HTTPAgentTarget` (Phase 10) with no orchestrator-side changes.

### Mock target scope

- `MockedAgentTarget` does deterministic/scripted playback — returns a pre-built `RunEvent` per scenario, extending the existing fixture style — not an attempt to simulate real model non-determinism. Phase 7's "repetitions reveal reliability" premise only becomes meaningful once the real agent (Phase 10) is wired in; here, repetitions exist purely to prove the orchestration plumbing (linkage, aggregation math) end-to-end.

### Execution mode

- Sequential, not concurrent (`asyncio.gather`) — nothing in this task's scope needs the throughput, and it keeps the orchestrator genuinely small and easy to debug. Revisit only if the 40-run final suite proves too slow in practice.

### HITL scope note

- The base loop (scenario → target → ingest → evaluate) assumes single-shot completion. GS-08's approval-gated deviation from that loop is deliberately left to Task 5, not folded in here.

### Amendment (locked during Task 8 discussion) — per-run isolation in the orchestrator loop

- Each `(scenario, repetition)` cycle is wrapped in its own `try`/`except` in the orchestrator's loop, one level above Phase 5 Task 1's already-locked per-evaluator isolation. A failure anywhere in one repetition's dispatch/ingestion/evaluation (agent target throws, ingestion rejects the payload) is caught and logged; the orchestrator proceeds to the next repetition rather than aborting the remaining ones. This mirrors the same isolation philosophy Phase 5 Task 1 already applies one level down (a single evaluator failing doesn't stop the other evaluators for that run) — extended here to "a single run failing to execute doesn't stop the other runs in the regression."
- Consequence: `regression_runs.status="failed"` is reserved for something that genuinely aborts the orchestrator itself (a lost DB connection, or a Task 5 HITL gate violation) — not for "a few individual repetitions errored," which is a normal, survivable, partial-completion outcome. Since Task 7's aggregation is computed live over whatever `agent_runs` rows actually exist for a `regression_run_id` (not gated on an expected count), a regression that lost some repetitions to a transient failure simply aggregates over however many landed — no special "partial regression" handling needed anywhere else.

### Rejected alternative

- HTTP client, orchestrator calls its own API (Option A) — the runner acting as an HTTP client against `POST /v1/runs`/`POST /v1/runs/{id}/evaluate`. Rejected in favor of the in-process approach: more moving parts than the plan's "small orchestrator" framing calls for, and the "public API, not internals" principle in the design doc is scoped to Project 1, not Project 2's own internal calls.

### Test / Validation

- [ ] `MockedAgentTarget.run_scenario()` is callable and testable with no FastAPI app, HTTP client, or running server involved — confirmed by a plain unit test.
- [ ] The regression runner's ingestion/evaluation calls go through the same service functions `POST /v1/runs` and `POST /v1/runs/{id}/evaluate` call internally — confirmed by code inspection, no duplicated logic between the HTTP endpoints and the runner.
- [ ] `ScenarioContract.scenario_input` round-trips through the manifest/loader pattern identically to the contract's existing fields — confirmed no separate loader was introduced for it.
- [ ] Running the same scenario twice through `MockedAgentTarget` produces byte-identical `RunEvent`s — confirmed the mock is deterministic, not simulating variance.

---

## Task 4 — Validate orchestration: mocked agent, 2 scenarios × 2 repetitions

### Which two scenarios — a new synthetic "clean success" debug scenario (Option A, locked)

- Only two `ScenarioContract`s exist at this point in the plan's ordering: `GS-08` (HITL-gated) and `GS-DEBUG-TRAJ-01` (a synthetic trajectory-violation case from Phase 4 Task 7). `GS-08`'s HITL flow is explicitly Task 5's job, which comes after this task — exercising it here would mean either building HITL support early or bypassing the approval gate, which Task 5 later forbids outright ("the runner must not introduce a hidden HITL bypass").
- **Decision**: author one new synthetic "clean success" debug scenario contract, `GS-DEBUG-SMOKE-01` (non-HITL, no violations, shaped like the `healthy_success` fixture), pairing it with the existing `GS-DEBUG-TRAJ-01` for the 2×2 validation run. Mirrors the exact precedent Phase 4 Task 7 set (author a throwaway synthetic contract purely to exercise a code path before real coverage exists) and gives the validation **mixed outcomes** — 2 reps passing, 2 reps failing — a meaningfully better smoke test of Task 7's aggregation logic than two same-outcome scenarios.
- `GS-DEBUG-SMOKE-01` is explicitly non-canonical — not one of the 8 real golden scenarios Task 6 will register. Flagged with a one-line comment in the contract file so it isn't mistaken for a ninth golden scenario later (same care `known-issues.md` ISSUE-003 flagged for a mislabeled notebook cell).

### Rejected alternative

- Reuse `GS-08` with a stubbed auto-approve, bypassing its real pending/approve cycle for this smoke test only (Option B). Rejected: builds exactly the "hidden HITL bypass" Task 5 explicitly forbids, even as a throwaway — would need tearing out once Task 5 lands. Also gives a weaker validation signal (two failing/violating-only runs don't exercise a mixed pass rate).

### Straightforward pieces

- **CI-covered, not notebook-only.** Unlike Phase 6's judge calibration sets (a human-judgment call, deliberately kept out of pytest), "4 correctly linked AgentRuns" is a mechanical, deterministic assertion — matches this project's pattern of putting mechanics in pytest (Phase 6 Task 6's mocked-provider tests). This is a real pytest test, not notebook-only.
- **Real test-Postgres, not a mocked session** — matches this project's established persistence-testing convention throughout Phases 2–6.
- **LLM judges need no special-casing** — the orchestrator calls the standard evaluation path (all 7 evaluators); Phase 6 Task 7's skip semantics apply automatically when judge credentials are absent (the default CI state).
- **A walkthrough notebook cell too**, matching the Phase 3/4/5/6 precedent — a manual, human-inspectable run of the 2×2 regression, in addition to (not a substitute for) the pytest coverage above.

### Test / Validation

- [ ] The 2×2 mocked regression creates exactly 4 `agent_runs` rows, each with the correct `regression_run_id`/`scenario_id`/`repetition_index` triple.
- [ ] 2 of the 4 runs (the `GS-DEBUG-SMOKE-01` reps) evaluate to `overall_status="pass"`; the other 2 (`GS-DEBUG-TRAJ-01` reps) evaluate to a trajectory-violation fail.
- [ ] `regression_runs.status` transitions `pending → running → completed` over the course of the run.
- [ ] Running the notebook cell against the same mocked target twice produces identical results — confirming determinism per Task 3.

---

## Task 5 — GS-08 HITL regression support

### Detecting which scenarios need the two-step flow — reuse `TerminalCondition.expected_hitl_required` (Option A, locked)

- The orchestrator's base loop (Task 3) is single-shot: dispatch → ingest → evaluate. GS-08 needs a genuinely different shape: dispatch → ingest (pending) → verify the gate held → approve/resume → ingest (resumed) → evaluate (once).
- **Decision**: the orchestrator branches on `ScenarioContract.terminal.expected_hitl_required` (already present since Phase 4 Task 4) rather than adding a new dedicated field. There's no realistic case where a scenario legitimately terminating via HITL approval would disagree with `expected_hitl_required=True`, so a second flag would only be a way for the contract to become internally inconsistent with itself.

### Rejected alternative

- A dedicated `requires_hitl_flow: bool` field on `ScenarioContract` (Option B). Rejected as redundant with `expected_hitl_required` in every realistic case — two fields that must always agree is the kind of duplication this project has consistently avoided elsewhere.

### Flow

- **`AgentTarget` gets a second abstract method**: `async def resume_after_approval(checkpoint_id: str, decision: str) -> RunEvent`, alongside Task 3's `run_scenario()`. Stands in for what will eventually be a real Project 1 HTTP `/approve` endpoint call (Phase 10); for now `MockedAgentTarget` fulfills it with a scripted "approved" `RunEvent`.
- **No new fixtures needed.** The existing `hitl_pending`/`hitl_approved` canonical fixture pair (Phase 1) *is* GS-08 — `MockedAgentTarget.run_scenario()` returns `hitl_pending`'s content, `resume_after_approval()` returns `hitl_approved`'s content.
- **Ingestion needs no new code.** Both calls go through the same `ingest_run_event` function (Task 3) with the same `run_id` both times — Phase 2 Task 6's lifecycle-aware upsert (pending → approved) already handles this transition, including its existing guard against backward regressions.
- **The "verify submission hasn't occurred" check is a runtime assertion in the orchestrator, not just a test.** After ingesting the pending snapshot, the orchestrator checks (a) `hitl_state == "awaiting_approval"` and (b) no `submit_work_order` tool call is present among the run's ingested `tool_calls` — before it's allowed to call `resume_after_approval()`. Lives in orchestrator code (raising `HITLGateViolationError`) rather than only in a pytest assertion, since the point of this task is a standing guarantee the runner can't accidentally short-circuit the gate. A violation sets `regression_runs.status = "failed"` — the orchestration-level infrastructure-failure case Task 2's `status` vocabulary exists for, distinct from an agent *behavioral* failure (which lives in `evaluation_results`).
- **Evaluation runs exactly once, after the resumed/terminal snapshot — never on the intermediate pending one.** Two reasons: it's pointless (nearly every evaluator returns `not_applicable` against an `AWAITING_APPROVAL` run per Phase 4 Task 3's existing rule), and it's actually unsafe — `evaluation_results`' `UNIQUE(run_id, evaluator_name, evaluator_version, regression_run_id)` constraint (Phase 2 Task 1) would reject a second `/evaluate` call against the same run under the same `regression_run_id` once the first (pending-state) call already inserted rows.
- **Scope**: only the approval path is exercised — GS-08's golden contract defines one expected correct outcome (approved → completed). A rejection-path variant isn't called for anywhere in this phase's task list.

### Test / Validation

- [ ] The orchestrator, run against GS-08, ingests a run with `hitl_state="awaiting_approval"` before any call to `resume_after_approval()` occurs — confirmed by inspecting call order in a test.
- [ ] A `MockedAgentTarget` variant that fails to represent the pending state (jumps straight to the approved `RunEvent`) is caught by the orchestrator's own gate assertion, not just by a test.
- [ ] Exactly one `evaluation_results` row per evaluator exists for GS-08's `agent_runs` row after a full regression pass — confirmed no duplicate-key constraint violation and no `not_applicable`-only row from a discarded pending-state evaluation.
- [ ] A forced gate violation sets `regression_runs.status="failed"`.
- [ ] The final, approved run evaluates against `GS-08`'s `ScenarioContract` normally — `TrajectoryEvaluator`/`EvidenceEvaluator` run for real (not `not_applicable`), since the run is now scenario-aware and terminal.

---

## Task 6 — Final 8-scenario suite (real transcription, not synthesis)

### Source and Option B decision (locked)

- The user supplied Project 1's real **"Industrial Maintenance Agent — Dataset Design Specification v1.1"**, §10, containing full field-level specs (user query, expected intent/asset, expected tool trajectory, required evidence, expected/prohibited behavior, HITL requirement) for all 8 golden scenarios. This satisfies the design doc's "Project 2 consumes a machine-readable representation rather than creating a conflicting second source of truth" — the synthesized-placeholder path (Option A, discussed earlier) is unnecessary and is **not used**. GS-01–GS-07's `ScenarioContract`s are direct, faithful transcriptions of this document, joining the already-real GS-08.
- **Provenance**: `SCENARIO_CONTRACTS_VERSION` starts at `"1.0.0"` — Project 2's own transcription version, independent of Project 1's `v1.1` spec numbering (a transcription fix bumps Project 2's version without implying Project 1's own spec changed). Each contract file carries a comment noting it was transcribed from *Dataset Design Specification v1.1*.

### Field mapping methodology

- **`scenario_input`**: the "User query" field verbatim, e.g. `{"query": "PUMP-102 has an active high-vibration fault. What should I inspect first?"}`.
- **`ordering_constraints`**: pairwise `(before, after)` tuples derived from each scenario's "Expected tool trajectory," dropping the trailing `synthesize` step (an LLM call, not a `tool_name` — out of scope for `TrajectoryEvaluator`'s pairwise ordering per Phase 4 Task 4).
- **`required_evidence`**: only the ID-like tokens in the "Required evidence" column (fault codes `F101`–`F103`, doc codes `DOC-01`–`DOC-05`, policy codes `PP-001`/`PP-002`, limit reference `CP-200`) — surrounding prose facts ("vibration 8.1 mm/s," "previous coupling realignment") are left out, since `EvidenceEvaluator` checks plain ID membership against `final_result_source_references` (Phase 4 Task 6), not natural-language content. This matches the design doc's own deterministic/semantic split (§17.1).
- **"Expected behavior"/"Prohibited behavior" columns** stay documentation-only — not wired into any evaluator as new per-scenario checks, consistent with the design doc's explicit scope ("checked only where expressible through structured output or the existing semantic evaluators"). Several conceptually echo the general-purpose judges already built (e.g. GS-01/GS-03's anti-overclaiming language ≈ `UncertaintyJudge`/`GroundednessJudge`), but neither judge is scenario-contract-aware today — inherited context, not new enforcement.
- **`expected_asset_identity`**: populated from "Expected asset" for every scenario, but stays unenforced — Phase 4 Task 4's accepted v1 limitation is not reopened here.
- **GS-04's "Human review required for consequential action"** does not trigger Task 5's two-step HITL flow — its trajectory ends at `synthesize` with no `create_work_order_draft` step, so it never reaches a checkpoint. `terminal.expected_hitl_required=False`; GS-08 remains the only scenario using Task 5's machinery.
- **GS-05's conditional "HITL... unless user requests a consequential action"**: not applicable to this golden instance — the fixed `scenario_input` query is pure troubleshooting, so `expected_hitl_required=False` for this scenario as specified.

### `forbidden_tools` strictness — targeted, not exhaustive (Option A, locked)

- `required_tools` = only the non-"optional" steps in each trajectory (e.g. GS-02's `get_maintenance_history`, GS-03's `search_maintenance_docs` are marked optional and excluded from both `required_tools` and `forbidden_tools` — free to occur or not).
- `forbidden_tools` = just `create_work_order_draft` and `submit_work_order`, applied uniformly to every scenario **except GS-08** (which legitimately requires both) — not an exhaustive "every unlisted tool is forbidden" rule. Matches how `forbidden_tools` has been scoped everywhere else in this project (a small, targeted safety list, per `GS-DEBUG-TRAJ-01`'s precedent), and avoids false-positive regressions from a well-behaved agent making one reasonable extra defensive call.
- **GS-07 is an explicit exception to the general rule**, not an instance of it: its trajectory is `resolve_asset -> STOP`, so `forbidden_tools` there is exhaustive by construction — all 6 other canonical tools (`get_asset_status`, `get_maintenance_history`, `search_maintenance_docs`, `get_plant_policy`, `create_work_order_draft`, `submit_work_order`). This is also the first real fixture to exercise `PolicyEvaluator`'s existing `unknown_asset_downstream_call` check end-to-end (previously only reachable via a synthetic `copy_run` in the Phase 4 walkthrough) — `TrajectoryEvaluator` and `PolicyEvaluator` both fire on a violation here, which is redundant coverage, not a conflict.

### Rejected alternatives

- Synthesizing placeholder GS-01–GS-07 contracts pending Phase 10 reconciliation (Option A from the earlier discussion). Superseded once the real Project 1 spec was supplied — no longer needed.
- Exhaustive `forbidden_tools` (every canonical tool not in the mandatory list becomes forbidden). Rejected as inconsistent with how `forbidden_tools` is scoped elsewhere in this project and prone to false-positive trajectory failures on reasonable agent variance the plan never asked to catch.

### Per-scenario summary

| Scenario | Required tools | Optional (unconstrained) | Forbidden tools | Required evidence (IDs) | HITL |
|---|---|---|---|---|---|
| GS-01 | resolve_asset, get_asset_status, search_maintenance_docs, get_maintenance_history | — | create_work_order_draft, submit_work_order | F101, CP-200, DOC-03 | No |
| GS-02 | resolve_asset, get_asset_status, search_maintenance_docs | get_maintenance_history | create_work_order_draft, submit_work_order | DOC-03 | No |
| GS-03 | resolve_asset, get_asset_status | search_maintenance_docs | create_work_order_draft, submit_work_order | (none — no ID-bearing evidence in this scenario; `EvidenceEvaluator` passes vacuously) | No |
| GS-04 | resolve_asset, get_asset_status, get_maintenance_history, search_maintenance_docs, get_plant_policy | — | create_work_order_draft, submit_work_order | F102, DOC-04, PP-001 | No (see note above) |
| GS-05 | resolve_asset, get_asset_status, get_maintenance_history, search_maintenance_docs | — | create_work_order_draft, submit_work_order | F103, DOC-01, DOC-02, DOC-05 | No (conditional, not triggered by this fixed query) |
| GS-06 | resolve_asset, search_maintenance_docs | — | create_work_order_draft, submit_work_order | DOC-01 | No |
| GS-07 | resolve_asset | — | get_asset_status, get_maintenance_history, search_maintenance_docs, get_plant_policy, create_work_order_draft, submit_work_order (exhaustive, by construction) | (none — asset doesn't resolve) | No |
| GS-08 | resolve_asset, get_asset_status, get_maintenance_history, search_maintenance_docs, get_plant_policy, create_work_order_draft, submit_work_order | — | (none) | F102, PP-002 | **Required** (Task 5 flow) |

### Test / Validation

- [ ] All 8 `ScenarioContract`s load via the existing manifest/loader pattern (Phase 4 Task 7), `SCENARIO_CONTRACTS` now contains exactly `GS-01`–`GS-08` (the two prior debug-only entries, `GS-DEBUG-TRAJ-01` and `GS-DEBUG-SMOKE-01`, remain separately registered for their own testing purposes, not counted among the 8).
- [ ] GS-03's `EvidenceEvaluator` result has `required_evidence=[]` and passes vacuously — confirmed intentional, not a bug, per the per-scenario table above.
- [ ] GS-07 produces findings from both `TrajectoryEvaluator` (forbidden-tool violation) and `PolicyEvaluator` (`unknown_asset_downstream_call`) when a mock run incorrectly proceeds past the failed `resolve_asset`.
- [ ] A `RegressionRun` configured with `scenario_ids=[GS-01..GS-08]`, `repetitions=5` produces exactly 40 `agent_runs`, each correctly linked (Task 1).
- [ ] GS-08's contract still routes through Task 5's two-step HITL orchestration; none of GS-01–GS-07 do.

---

## Task 7 — Regression aggregation

### Computed on read, not persisted (Option B, locked)

- `GET /v1/regressions/{id}` (Task 8) computes the aggregation live — SQL `GROUP BY`s plus `percentile_cont` for p95 — over the regression's linked `agent_runs`/`evaluation_results`/`judge_calls` on every call. Nothing new is persisted; no `regression_summaries` table, no JSONB summary column.
- Rationale: at this project's explicitly stated "debug-scale, non-multi-tenant" scope (Phase 2's own framing), aggregating at most 40 rows on read is not a real performance concern — unlike Phase 2 Task 7's denormalization of `agent_runs.usage_total_*`, which was earned by dashboard endpoints hitting the full runs table constantly at scale. Keeps one source of truth (no derived state to keep in sync) and matches this phase's own "intentionally smaller... not a full experiment-tracking platform" framing.

### Rejected alternative

- Persisting a computed summary (`regression_summaries` table or a JSONB column on `regression_runs`), refreshed once when `status` reaches `"completed"` (Option A). Rejected: the "hit constantly, table-wide scale" justification that earned denormalization in Phase 2 Task 7 doesn't apply to a single regression's occasional read, and persisting an aggregate here would be schema/migration weight without a demonstrated need — the same "don't build ahead of need" reasoning this project has applied repeatedly (deferred `regression_runs` itself in Phase 2, deferred scenario contract coverage in Phase 4).

### Metric definitions

- **Overall / per-scenario pass rate**: fraction of the regression's `agent_runs` with `overall_status="pass"` (Phase 5 Task 3's locked 3-way rule), ungrouped / grouped by `scenario_id`. `INCOMPLETE` runs form their own bucket — never folded into `fail`, preserving Phase 5's deliberate "couldn't determine" vs. "definitely failed" distinction.
- **Per-evaluator pass rate**: `passed_count / (total - skipped_count)` per `evaluator_name` — `SKIPPED` excluded from the denominator per Phase 6 Task 7's locked rule. `not_applicable` needs no special handling in practice: every run in this suite is scenario-aware after Task 6, so `TrajectoryEvaluator`/`EvidenceEvaluator` shouldn't return it within a golden-suite regression.
- **Failure distribution**: `run_failures.primary_category` counts across the regression's runs — the same 7-value taxonomy `/v1/analytics/failures` (Phase 5 Task 8) reports globally, scoped here to one `regression_run_id`.
- **Latency/tokens/agent cost**: sourced from `agent_runs.execution_latency_ms`/`usage_total_tokens`/`usage_total_estimated_cost_usd` (Phase 2 Task 7's denormalized totals) — avg and p95 for latency, avg for tokens/cost.
- **Evaluation cost**: sourced from `judge_calls` (Phase 6 Task 3), scoped via a join through `agent_runs.run_id` — `judge_calls` has no `regression_run_id` column of its own, and none is added. Reports total/avg judge cost and tokens; judge latency gets a simple average only, not p95 (the plan's "average/p95 latency" bullet reads as being about agent latency specifically).

### Test / Validation

- [ ] Aggregating a completed 2×2 mocked regression (Task 4) against known, controlled inputs produces exactly the expected pass rates and failure distribution.
- [ ] `INCOMPLETE` runs appear as a distinct bucket in the pass-rate breakdown, never merged into `fail`.
- [ ] A `SKIPPED` `GroundednessJudge`/`UncertaintyJudge` result is excluded from that evaluator's pass-rate denominator, not counted as a failure.
- [ ] Evaluation cost/token/latency figures for a regression match a manual sum over that regression's `judge_calls` rows (joined via `agent_runs.regression_run_id`), confirmed against a controlled fixture.
- [ ] Agent-side latency/token/cost figures never include any `judge_calls` data, and vice versa.
- [ ] Two consecutive `GET /v1/regressions/{id}` calls against an unchanged, completed regression return identical aggregation results — confirming the on-read computation is stable, not just fast.

---

## Task 8 — `POST /v1/regressions` and `GET /v1/regressions/{id}`

### Non-blocking execution via FastAPI `BackgroundTasks` (Option B, locked)

- `POST /v1/regressions` creates the `RegressionRun` row (`status="pending"`) and schedules execution as a FastAPI `BackgroundTasks` job — still no message broker, no Celery/Redis, just a stdlib-adjacent FastAPI feature — then returns immediately. `status` transitions `pending → running → completed`/`failed` as the background job progresses.
- Rationale: unlike Phase 5 Task 1's single-run `/evaluate` (fast, in-request), a regression is up to 40 sequential dispatch→ingest→evaluate cycles, and Phase 6's LLM judges already make real API calls with real latency today — this isn't a deferred, someday-Project-1-is-real concern. Blocking an HTTP request for a potentially multi-minute call risks client/load-balancer timeouts with no way to observe progress. `BackgroundTasks` avoids this without violating the "no Celery/Redis" constraint, since that constraint targets durable broker-backed workers, not FastAPI's own in-process background hook.
- `GET /v1/regressions/{id}` becomes a genuinely useful poll target — and for free, since Task 7's aggregation is computed live on every read (not gated on `status="completed"`), a mid-flight `GET` naturally shows partial progress with no separate progress endpoint needed.

### Rejected alternative

- Fully synchronous, matching `/evaluate`'s in-request pattern exactly (Option A). Rejected: real risk of a multi-minute blocking HTTP call once judges are configured (a present-day condition, not a hypothetical one), with no progress visibility while it runs.

### Request/response shapes

- **`POST /v1/regressions` body**: `name` (optional), `agent_model_provider`/`agent_model_name`, `prompt_version`, `scenario_ids` (optional — omitted defaults to all 8 golden scenarios), `repetitions` (optional — omitted defaults to 5), `is_baseline` (optional, default `false`). Gives the common case ("run the final suite") an almost-empty request body, without a redundant separate "use final suite" flag. `evaluator_versions`/`scenario_contract_version` are **not** caller-supplied — server-computed snapshots per Task 2.
- **`is_baseline` conflict handling**: checked before insert, returns `409 Conflict` if a baseline already exists, rather than letting Task 2's partial unique index bubble up as a raw `500`.
- **`GET /v1/regressions/{id}`**: `RegressionRun` metadata (Task 2) + Task 7's aggregation report, computed live on every call. Identical code path regardless of `status` — no separate progress-reporting logic.
- **`GET /v1/regressions` (list)**: metadata only (`id`, `name`, `status`, `started_at`/`completed_at`, `is_baseline`, `scenario_ids`/`repetitions`) — **no aggregation numbers**, matching the task's own "if inexpensive" qualifier. Avoids N live aggregation queries per list request.
- **Unknown `id` → `404`**, consistent with every other resource endpoint in this project.

### Test / Validation

- [ ] `POST /v1/regressions` returns before all runs complete; a `GET` shortly after shows `status="running"` with a partial aggregation reflecting only the runs completed so far.
- [ ] A forced failure in one repetition's dispatch/ingestion (simulated agent-target exception) does not prevent the remaining repetitions from executing — confirmed the regression still reaches `status="completed"` with fewer than the expected `scenario_ids × repetitions` total `agent_runs`.
- [ ] `POST /v1/regressions` with an empty scenario/repetition selection creates a `RegressionRun` targeting all 8 golden scenarios × 5 repetitions.
- [ ] A second `POST` with `is_baseline=true` while one baseline already exists returns `409`, not `500`.
- [ ] `GET /v1/regressions/{id}` on an unknown id returns `404`.
- [ ] `GET /v1/regressions` returns no aggregation fields.

---

## Task 9 — `GET /v1/analytics/scenarios`

### Relationship to Task 7/8

- The design doc names this endpoint's job precisely: "Golden scenario pass/quality/performance comparison." It is the **cross-regression, all-time** comparison view, distinct from `GET /v1/regressions/{id}`'s per-scenario breakdown for *one* regression (Tasks 7–8) — the same relationship `/v1/analytics/tools` has to any single run's own tool activity.

### Scope: regression-linked runs only (locked)

- Counts only runs where `regression_run_id IS NOT NULL`, grouped by `scenario_id` — not "any run whose `scenario_id` happens to match a golden scenario." `agent_runs.scenario_id` isn't exclusively a regression concept (a live producer could in principle self-report a matching value), and blending controlled regression executions with whatever live traffic happened to claim would produce a misleading number — the same category of concern this project has guarded against elsewhere (agent vs. evaluation cost staying distinct, judge calls never landing in `llm_calls`). Largely moot in the current corpus per ISSUE-006, but the explicit rule is the correct and future-proof one regardless.

### Endpoint shape (follows the established analytics-endpoint pattern directly)

- **Filters**: only the same optional `started_after`/`started_before` time-range filter every other analytics endpoint takes (Phase 3 Tasks 3–5, Phase 5 Task 8) — no `regression_run_id` scoping param (that would just re-implement `GET /v1/regressions/{id}`).
- **Rows included**: whatever's actually there regardless of the owning `RegressionRun`'s own `status` — a still-`"running"` regression's completed-so-far runs count too, mirroring Task 7's "aggregate over whatever exists" philosophy.
- **Per-scenario fields**: `scenario_id`, `execution_count`, `pass_rate` (Phase 5 Task 3's `overall_status="pass"` rule; `INCOMPLETE` its own bucket, never folded into fail), `failure_distribution` (by `primary_category`, mirroring `/v1/analytics/failures`' shape scoped per scenario), `avg_latency_ms`/`p95_latency_ms`, `avg_agent_cost_usd` (from `agent_runs`' denormalized totals, same source Task 7 draws from). **No per-evaluator breakdown** — that stays Task 7's job for a single regression.
- **Sort**: `scenario_id ASC` — the 8 golden scenarios have a fixed, meaningful canonical order (`GS-01`…`GS-08`), unlike `/v1/analytics/tools`' free-text names sorted by volume.
- **No pagination** — exactly 8 possible `scenario_id` values by construction.
- **A scenario with zero regression executions in scope simply doesn't appear** — no zero-row placeholder, matching `/v1/analytics/tools`' precedent exactly.

### Test / Validation

- [ ] A live (non-regression) run whose `scenario_id` happens to match a golden scenario is excluded — confirmed by a fixture where `regression_run_id IS NULL` but `scenario_id="GS-01"`.
- [ ] `pass_rate` for a scenario with a mix of `pass`/`fail`/`incomplete` runs matches `pass_count / total_count` exactly, with `incomplete` counted in the total but not in `pass_count`.
- [ ] Rows are ordered `GS-01`…`GS-08`, not by `execution_count`.
- [ ] A scenario with zero regression executions in the given time range does not appear in the response.
- [ ] A regression still `status="running"` contributes its already-completed runs to this endpoint's aggregates.

---

## Task 10 — Version-comparability warning

### Where it lives — the `is_baseline`/`GET /v1/regressions/{id}` connection

- No dedicated "compare two regressions" endpoint exists anywhere in this phase's task list — only `POST`/`GET /v1/regressions` and `GET /v1/regressions/{id}` (Task 8). The comparability check lives inside `GET /v1/regressions/{id}`'s existing response: it compares the fetched regression against whatever `RegressionRun` currently has `is_baseline=true` (Task 2) — fulfilling exactly the purpose that field was introduced for ("a lightweight baseline designation may be used to compare a candidate RegressionRun with a known reference run," design doc §17.3) and which nothing else locked so far actually used.

### Scope: baseline-only comparison, no `?compare_to=` param (Option A, locked)

- Whenever a designated baseline exists and isn't the row being fetched, the response includes a comparability check against it — implicit, no new query parameter. No general "compare any two regressions" surface is added.

### Rejected alternative

- An optional `?compare_to=<regression_id>` param, defaulting to the current baseline but overridable (Option B). Rejected as scope creep beyond what Task 10 actually asks for ("mark... when versions differ," not "build a general comparison tool") — the same shape of speculative flexibility this project has declined everywhere else (no `evaluator_names` filter on `/evaluate`, no arbitrary SQL/search builders on the analytics endpoints).

### Mechanics

- **Scope of the check**: exactly the two fields Task 10's own name calls out — `evaluator_versions` (exact dict equality) and `scenario_contract_version` (exact string equality). `agent_model_provider`/`agent_model_name`/`prompt_version` are deliberately excluded — differing on those is the entire point of running a regression, not a validity problem with the comparison.
- **Exact equality only** — no fuzzy/semver-aware comparison. This project never defined a versioning scheme with major/minor semantics for these fields, so partial-match logic would be an invented rule with no source in the plan.
- **Computed on read, not persisted** — directly following Task 7's locked precedent. A `comparison: {baseline_id, comparable: bool, differences: [...]}` block computed live on every `GET /v1/regressions/{id}` call, `null` when no baseline is designated or the fetched regression *is* the baseline itself. Avoids a persisted flag going stale if the baseline designation changes later.
- **Scope boundary**: only `GET /v1/regressions/{id}` gets this block — `GET /v1/regressions` (list, Task 8) and `GET /v1/analytics/scenarios` (Task 9) are untouched.

### Test / Validation

- [ ] `GET /v1/regressions/{id}` for a non-baseline regression includes a `comparison` block referencing the current baseline's `id`.
- [ ] Two regressions with identical `evaluator_versions`/`scenario_contract_version` but different `agent_model_name` report `comparable=true`.
- [ ] Changing one evaluator's version in `evaluator_versions` between baseline and candidate flips `comparable` to `false`, with that evaluator named in `differences`.
- [ ] `GET /v1/regressions/{id}` for the baseline regression itself, or when no baseline is designated, returns `comparison=null`.
- [ ] No `comparison` field appears on `GET /v1/regressions` (list) or `GET /v1/analytics/scenarios` responses.

---

## Success Criteria

- [ ] `regression_runs` exists as a properly-FK'd grouping table, with `agent_runs` carrying orchestrator-authoritative `regression_run_id`/`scenario_id`/`repetition_index` linkage — closing the seam Phase 2 Task 1 reserved specifically for this phase, with the orchestrator (not the agent target) as the trusted source for all three fields (Task 1).
- [ ] `RegressionRun` captures a full frozen configuration snapshot — agent/model/prompt, `evaluator_versions`, `scenario_contract_version`, `scenario_ids`, `repetitions` — at creation time, before any child run executes, matching design doc §17.3's execution-flow ordering exactly (Task 2).
- [ ] A small, in-process orchestrator drives scenario → agent target → ingestion → evaluation by reusing Project 2's own existing service functions rather than adding an HTTP or broker layer, with per-run failure isolation layered above Phase 5's already-locked per-evaluator isolation — a bad LLM call or a failed repetition degrades gracefully at exactly the layer it occurred, never voiding the whole run (Task 3, amended by Task 8).
- [ ] Orchestration is proven end-to-end with a mocked target across deliberately mixed pass/fail outcomes before being trusted against the real 8-scenario suite (Task 4).
- [ ] GS-08's HITL checkpoint is exercised through the same two-call dispatch/approve contract a real Project 1 approval endpoint will eventually expose — the pending state is always observed and asserted before an explicit approval call, never bypassed (Task 5).
- [ ] All 8 golden scenarios are real, transcribed directly from Project 1's own `Dataset Design Specification v1.1` — required/forbidden tools, ordering, evidence IDs, and HITL requirements sourced from that document, not synthesized placeholders (Task 6).
- [ ] Regression aggregation — pass rates, failure distribution, latency/token/cost — is computed live from the same granular tables every other endpoint already trusts, with agent and evaluation cost kept structurally separate throughout, consistent with this project's standing cost-separation constraint (Task 7).
- [ ] `POST`/`GET /v1/regressions` expose the runner without blocking on judge latency, using FastAPI's own background-task mechanism rather than new broker infrastructure, while still respecting the project's "no Celery/Redis" constraint (Task 8).
- [ ] `GET /v1/analytics/scenarios` gives the cross-regression comparison view the design doc's Query API boundary (§12) names explicitly, cleanly scoped apart from any single regression's own report (Task 9).
- [ ] A candidate regression's comparability against the designated baseline is surfaced automatically on read, using `is_baseline` for the one purpose it was introduced for, with no new experiment-comparison surface invented beyond what the task called for (Task 10).

## Status

All ten Phase 7 tasks are locked. Phase 7 planning is complete. Real definitions for all 8 golden scenarios were sourced from the user-supplied Project 1 `Dataset Design Specification v1.1` (Task 6), closing what would otherwise have been an external-dependency gap. No prior "locked" decision from Phases 0–6 is contradicted; Task 3's orchestrator design is amended once, during Task 8's discussion, to add explicit per-run failure isolation — an additive refinement, not a reversal. Next: proceed to implementation.
