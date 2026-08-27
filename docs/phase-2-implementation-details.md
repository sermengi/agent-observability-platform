# Phase 2 (Ingestion & Persistence) — Implementation Decisions

Captured from planning discussion, 2026-08-27. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 2 and building on the Phase 0 (Walking Skeleton) and Phase 1 (Telemetry Contract & Debug Fixtures) decisions without contradicting either. Decisions are locked task-by-task, following the Phase 2 task list from the implementation plan:

1. Create relational models/migrations for `agent_runs`, `spans`, `tool_calls`, `llm_calls`, `evaluation_results`, `run_failures`, and the future `regression_runs` relationship. **(locked)**
2. Use relational columns for IDs, timestamps, statuses, versions, names, latency, tokens, cost, scenario IDs, and failure fields; use JSONB for variable payloads/findings/metadata. **(locked)**
3. Implement `POST /v1/runs` with schema validation and normalization. **(locked)**
4. Implement stable external run/child ID mapping. **(locked)**
5. Implement idempotent repeated ingestion so duplicate snapshots do not create duplicate runs/calls. **(locked)**
6. Implement lifecycle-aware upsert for HITL pending → approved/rejected/completed snapshots. **(locked)**
7. Add derived/denormalized run usage totals while keeping granular calls as the source of truth. **(locked)**
8. Add the minimal indexes required for run lookup, status/scenario/version filtering, tool/model analytics, and evaluator/failure lookup. **(locked)**

All eight Phase 2 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0 and 1 were planned. See Success Criteria and Status at the bottom.

---

## Task 1 — Relational models/migrations for the core entities

### Scope framing

- Phase 0 already wired Alembic's async template and imported `Base.metadata` with zero domain models. This task is "write real ORM models, then run `alembic revision --autogenerate`" — no hand-written DDL, no new migration tooling decisions.
- Phase 1 already locked that `span_id`/`tool_call_id`/`llm_call_id` are unique only **within their parent run**, not globally (Phase 1 Task 2) — every child table's natural key is a composite `(run_id, <child_id>)`, not the bare child ID. This is a constraint inherited from upstream, not re-decided here.

### Module layout (straightforward)

- One `models.py` under a new `src/obs_platform/db/` package — mirrors Phase 1's "few tightly-coupled types, one file" precedent; six tables isn't enough to justify a split-by-entity layout yet.

### Primary key strategy — a hybrid, chosen per-table by whether anything references the row

- **`agent_runs`**: `run_id` (string) is the actual PK — already globally unique per Phase 1, no surrogate needed.
- **`spans`**: surrogate integer PK (`id`) plus `UNIQUE(run_id, span_id)`. This is the only child table with a self-reference (`parent_span_id`) and with two other tables (`tool_calls`, `llm_calls`) pointing into it — so it's the only table where a composite FK would actually be avoided by a surrogate key.
- **`tool_calls` / `llm_calls`**: natural composite PK (`run_id, tool_call_id`) / (`run_id, llm_call_id`) directly. Nothing references these rows, so a surrogate key would earn nothing — rejected adding one purely for PK-style consistency, consistent with this project's established taste for minimal ceremony (e.g. Phase 6 rejecting a dedicated Postgres sequence for `work_order_id` because nothing in that project's usage pattern needed it).

### `ErrorInfo` representation — flattened relational columns, not JSONB

- `error_category`, `error_code`, `error_message`, `error_failed_component` as real columns on `spans`, `tool_calls`, `llm_calls`, and `agent_runs.runtime_error_*` — directly required by this phase's own Task 2 instruction to use relational columns for "failure fields," and serves Phase 3's per-tool failure-rate analytics (`GET /v1/analytics/tools`) with a plain `GROUP BY error_category` rather than JSON path extraction.

### `regression_runs` — deferred entirely, with one reserved seam

- No `regression_runs` table is built in this phase — its actual columns (frozen agent/model/prompt/evaluator config) aren't designed until Phase 7, and Phase 0's own "no placeholder domain schema" precedent applies directly: guessing the shape now risks having to unwind it later.
- One concession, resolved as an amendment during this task's discussion: `evaluation_results.regression_run_id` is added now as a bare, nullable `INTEGER` column with **no FK constraint** (since `regression_runs` doesn't exist yet) — it sits unused (`NULL`) until Phase 7 both creates the table and adds the FK via an additive migration. This was necessary to reconcile with the `evaluation_results` uniqueness grain decision below, which needs the column to exist now.

### `evaluation_results` — append/history grain, not latest-only

- Surrogate PK `id`; `UNIQUE(run_id, evaluator_name, evaluator_version, regression_run_id)`. Every evaluator invocation inserts a new row rather than overwriting a `(run_id, evaluator_name)` row in place.
- Rejected alternative: `UNIQUE(run_id, evaluator_name)` with upsert-in-place (mirroring `run_failures`). Rejected because Phase 7 ("Regression Evaluation") exists specifically to re-run the same golden scenarios' evaluators repeatedly to detect regressions over time — a latest-only grain would either destroy that history on re-evaluation or force a breaking `UNIQUE`-constraint migration later. `evaluator_version` was already a named field in the design doc's own model table, so "versioned" was never in question.
- Postgres treats multiple `NULL`s in `regression_run_id` as distinct by default, so ad-hoc (pre-Phase-7) re-evaluations naturally insert additional rows with no special-casing needed. "Latest result for this run/evaluator" becomes a query (`ORDER BY created_at DESC LIMIT 1`), not a constraint-enforced fact — acceptable since that's a read-path concern for whichever phase queries it, not a schema risk now.

### Enum representation — `TEXT` + `CHECK`, not native Postgres `ENUM`

- All controlled-vocabulary columns whose values are already locked by Phase 1 (`RunStatus`, `RunEventType`, `ExecutionStatus`, `HITLState`, `LLMCallType`, `HITLInfo.decision`) are `TEXT` with a `CHECK` constraint, not a native PG `ENUM` type — mirrors Phase 6's `work_orders.status` CHECK-constraint precedent directly (a CHECK constraint is simpler to widen later than altering a PG enum type).
- Columns whose vocabulary is **not yet defined** — `evaluation_results.status`/`score`/`label`/`severity` and `run_failures.primary_category`/`secondary_category`/`max_severity` — stay plain `TEXT`, **unconstrained**. Phase 4/5 haven't built the evaluators or failure taxonomy that would define these value sets yet; this mirrors the same distinction Phase 1 already drew for `RunStatus` itself (runtime status vs. a future evaluator's conclusion).

### Concrete schema

**`agent_runs`** (PK: `run_id`)
`run_id`, `schema_version`, `event_type` (CHECK), `agent_name`, `agent_version`, `prompt_version`, `environment`, `raw_input` (JSONB), `normalized_input`, `scenario_id`, `started_at`, `completed_at`, `status` (CHECK), `execution_latency_ms`, `wall_clock_duration_ms`, `resume_count`, `hitl_required`, `hitl_state` (CHECK), `hitl_checkpoint_id`, `hitl_decision` (CHECK), `hitl_requested_at`, `hitl_decided_at`, `hitl_pending_action` (JSONB) — `HITLInfo` flattened onto the run row since it's a single embedded object per run, not a repeated child; `usage_total_llm_calls`, `usage_total_tool_calls`, `usage_total_tokens`, `usage_total_retries` (INTEGER, default 0), `usage_total_estimated_cost_usd` (DOUBLE PRECISION, default 0) — columns exist now, populated by Task 7, not this task; `final_result_output` (JSONB), `final_result_source_references` (native `TEXT[]`); `runtime_error_category`/`runtime_error_code`/`runtime_error_message`/`runtime_error_failed_component`; `ingested_at`, `updated_at` (ingestion bookkeeping, separate from the domain's own `started_at`/`completed_at`).

**`spans`** (surrogate PK `id`, `UNIQUE(run_id, span_id)`)
`id`, `run_id` (FK), `span_id`, `parent_span_id` (self-FK to `spans.id`, nullable), `name`, `sequence`, `started_at`, `completed_at`, `status` (CHECK), `input`/`output`/`metadata` (JSONB, nullable), `error_category`/`error_code`/`error_message`/`error_failed_component`.

**`tool_calls`** (natural PK `(run_id, tool_call_id)`)
`run_id` (FK), `tool_call_id`, `span_id` (FK to `spans.id`), `tool_name`, `sequence`, `arguments` (JSONB), `result` (JSONB, nullable), `started_at`, `completed_at`, `latency_ms`, `retry_count`, `status` (CHECK), `error_*`.

**`llm_calls`** (natural PK `(run_id, llm_call_id)`)
`run_id` (FK), `llm_call_id`, `span_id` (FK), `call_type` (CHECK), `model`, `provider`, `started_at`, `completed_at`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `estimated_cost_usd` (DOUBLE PRECISION), `input_payload`/`output_payload` (JSONB, nullable), `status` (CHECK), `error_*`.

**`evaluation_results`** (surrogate PK `id`, `UNIQUE(run_id, evaluator_name, evaluator_version, regression_run_id)`)
`id`, `run_id` (FK), `evaluator_name`, `evaluator_version`, `regression_run_id` (bare nullable INTEGER, no FK yet), `status` (TEXT, unconstrained), `score` (nullable), `label` (nullable, TEXT unconstrained), `severity` (nullable, TEXT unconstrained), `reason` (TEXT), `findings` (JSONB), `created_at`.

**`run_failures`** (1:1 with `agent_runs`, PK/FK `run_id`)
`run_id` (PK+FK), `primary_category` (TEXT, unconstrained), `secondary_category` (nullable, TEXT unconstrained), `max_severity` (TEXT, unconstrained), `updated_at`.

- Single `public` Postgres schema, no dedicated schema namespace — consistent with the project's debug-scale, non-multi-tenant framing.

### Test / Validation

- [ ] `alembic revision --autogenerate` run against these six models produces the expected `CREATE TABLE` set with no manual DDL edits required.
- [ ] `spans` has a surrogate integer PK and a `UNIQUE(run_id, span_id)` constraint; `tool_calls`/`llm_calls` have natural composite PKs with no surrogate `id` column — confirmed by schema inspection.
- [ ] `agent_runs.runtime_error_category`, `spans.error_category`, `tool_calls.error_category`, `llm_calls.error_category` all exist as real columns (not nested inside a JSONB `error` blob).
- [ ] `evaluation_results` has no `UNIQUE(run_id, evaluator_name)` constraint — confirmed a test can insert two rows for the same `(run_id, evaluator_name)` with different `evaluator_version` or `regression_run_id` values without conflict.
- [ ] No `regression_runs` table exists after this phase's migrations; `evaluation_results.regression_run_id` exists as a nullable column with no foreign-key constraint.
- [ ] `agent_runs.status`, `event_type`, `hitl_state` reject an out-of-vocabulary value via their `CHECK` constraints; `evaluation_results.status`/`severity`/`label` and `run_failures.primary_category`/`secondary_category` accept arbitrary text with no such rejection.
- [ ] `llm_calls.estimated_cost_usd` and `agent_runs.usage_total_estimated_cost_usd` are `DOUBLE PRECISION`, not `NUMERIC`.

---

## Task 2 — Relational-vs-JSONB column-type policy

### Scope framing

Largely already satisfied by Task 1's concrete schema — this task's job was to confirm the policy was applied consistently and settle the couple of spots not yet fully specified.

### Resolved specifics

- **Cost columns are `DOUBLE PRECISION`, not `NUMERIC`.** Follows Phase 1's own reasoning directly: `estimated_cost_usd` is a plain Python `float` because "this is observability telemetry, not a billing ledger" — a fixed-precision `NUMERIC` column would misleadingly imply ledger-grade exactness Phase 1 explicitly disclaimed.
- **`CHECK` constraints apply only to vocabularies Phase 1 already locked**, not to every status-shaped column — see Task 1's enum-representation decision above for the full reasoning and the specific columns on each side of the line.
- Every other element of the stated policy (relational for IDs/timestamps/names/versions/scenario_id/latency/tokens; JSONB for arguments/result/payloads/metadata/findings/pending_action) is already reflected exactly as designed in Task 1's table list — nothing further to add.

### Test / Validation

- [ ] Every column identified in Task 1 as "relational" (IDs, timestamps, statuses, versions, names, latency, tokens, cost, scenario IDs, failure fields) is a scalar Postgres column type, not JSONB — confirmed by schema inspection.
- [ ] Every column identified as "variable" (arguments, result, input/output payloads, metadata, findings, pending_action, raw_input, final_result_output) is JSONB.
- [ ] `evaluation_results.status`/`severity`/`label` and `run_failures.primary_category`/`secondary_category`/`max_severity` have no `CHECK` constraint — confirmed a test can insert an arbitrary string into each without rejection.

---

## Task 3 — `POST /v1/runs` schema validation and normalization

### Scope framing

Narrower than it sounds: Task 4 owns ID-mapping mechanics and Task 5 owns idempotent-upsert mechanics. This task is the request/response contract, how validation happens at the boundary, and the function boundary between the route and persistence.

### Request validation — direct FastAPI typing

- `async def ingest_run(event: ExtendedRunEvent, session: AsyncSession = Depends(...))` — FastAPI/Pydantic validates automatically before the route body executes; a malformed payload gets FastAPI's default `422` with Pydantic v2's built-in per-field `loc`/`msg`/`type`/`input` detail, including nested list indices (e.g. `["body", "spans", 2, "tool_calls", 0, "latency_ms"]`).
- Rejected alternative: manual `model_validate()` inside an explicit `try/except ValidationError` with a custom error envelope. Rejected because nothing in the plan calls for a custom error shape or rejected-payload logging, and Pydantic's default detail is already sufficiently precise for the Phase 10 integration-reconciliation concern this decision was weighed against.
- Noted for later, not built now: if Phase 10's real integration work reveals a need for persistent server-side visibility into rejected payloads (FastAPI's default only puts error detail in the response to the caller, not in a durable log), that's an additive logging hook to add at that time — not a reason to build a custom envelope speculatively now.
- Noted, not reopened: Phase 1's `extra="ignore"` policy means a mistyped field name from a producer is silently dropped rather than flagged directly — it surfaces instead as a "missing required field" error, one step removed from "you typo'd this." This is an already-locked Phase 1 tradeoff (deliberate producer/consumer version-skew tolerance), not revisited here.

### Normalization function boundary

- The route stays thin: parse/validate the body, then hand the validated `ExtendedRunEvent` to an injectable ingestion service (a function/class taking `session: AsyncSession` + the validated event) that performs the actual normalization and persistence. Satisfies this phase's own constraint to "keep persistence interfaces independently testable from FastAPI," and mirrors Project 1's established pattern of pure functions taking `session: AsyncSession`.

### Transaction and error-handling posture

- **One atomic transaction per ingested run** — the run row plus all its spans/tool_calls/llm_calls are written in a single DB transaction; either the whole snapshot lands or none of it does.
- **DB errors bubble up, uncaught** — no special try/except around persistence failures; an unhandled exception becomes FastAPI's default `500`. Consistent with "ingestion failure is a Project 2 concern" being visible and debuggable rather than silently swallowed.

### Response shape

- **`200`, not `201`** — this is fundamentally an upsert-shaped endpoint (a HITL re-post targets the same `run_id`), so a single status code that doesn't need to branch on create-vs-update reads more honestly than forcing that distinction before Task 5 designs how it's even detected.
- Body: `{"run_id": ..., "event_type": ..., "status": ...}`, echoing the resulting lifecycle state. Room is left to add a `"was_created": bool`-style field once Task 5's upsert query is designed — not locked now.

### Test / Validation

- [ ] `POST /v1/runs` with a valid fixture payload returns `200` with `run_id`/`event_type`/`status` matching the ingested event.
- [ ] `POST /v1/runs` with a payload missing a required field returns FastAPI's default `422`, with the specific missing field identifiable in the response's `detail` list — no custom error envelope is introduced.
- [ ] A payload with an unrecognized extra field validates successfully with that field silently absent — re-confirms Phase 1's `extra="ignore"` policy holds through this endpoint.
- [ ] The ingestion logic lives in a function/class that can be called and tested directly with a test `AsyncSession`, with no FastAPI app or HTTP layer involved — confirmed by a test that does exactly this.
- [ ] A forced DB-layer error (e.g. a broken connection in a test double) during ingestion propagates as an unhandled exception surfaced as `500`, not swallowed or converted to a different status.
- [ ] A payload whose normalization fails partway through (e.g. a constraint violation on the third of five spans) leaves zero rows for that run committed — confirmed by inspecting the DB after a forced mid-transaction failure.

---

## Task 4 — Stable external run/child ID mapping

### Scope framing

Narrower than the name suggests: Task 1 already built the schema specifically around composite natural keys for this purpose, and Phase 1's own model validator already guarantees within-payload referential integrity (every `tool_call.span_id`/`llm_call.span_id` in an incoming payload is already confirmed to match a `span_id` present in that same payload's `spans` list, before this code runs). This task is the normalization *algorithm*, not re-validation.

### No separate ID-mapping table

- Resolution happens directly against the entity tables' own unique constraints (`spans.UNIQUE(run_id, span_id)`, the natural composite PKs on `tool_calls`/`llm_calls`) — a dedicated `id_mappings` registry table would be redundant infrastructure, and brushes against this phase's own instruction not to introduce extra generic tables beyond what's needed.

### Internal surrogate keys never leave the process

- `spans.id` is purely an implementation detail for FK wiring — the Query API (Phase 3) always surfaces the external `span_id` string, never the internal integer.

### Normalization algorithm — two-pass, order-independent, `RETURNING`-based

- **Pass 1**: a single bulk `INSERT ... ON CONFLICT (run_id, span_id) DO UPDATE ... RETURNING span_id, id` upserts *all* of a payload's spans in one round trip, returning the complete external→internal ID map — covering both brand-new spans and carried-over HITL re-emissions uniformly, with no special-casing needed for which case applies.
- **Pass 2**: uses that map to (a) `UPDATE` `parent_span_id` back onto the just-upserted spans, and (b) resolve `tool_calls.span_id`/`llm_calls.span_id` before upserting those tables.
- Rejected alternative: sequential single-span processing assuming parent-before-child ordering in the payload's `spans` list. Rejected because nothing in Phase 1's contract guarantees span list ordering — that would quietly impose a new, undocumented producer obligation. The two-pass approach is order-independent for the same code cost.

### No defensive full-restatement check

- No check verifying that a snapshot doesn't silently omit previously-known children (which would violate Phase 1 Task 7's "full restatement" producer contract). Trust the producer contract as stated — consistent with this project's repeated pattern of not building guardrails against currently-hypothetical failure modes nothing on the test list asks for.

### Test / Validation

- [ ] Ingesting `hitl_pending` then `hitl_approved` (Phase 1's fixture pair) results in every carried-over `span_id`/`tool_call_id`/`llm_call_id` resolving to the *same* internal row identity across both ingestions — confirmed by inspecting `spans.id` before and after the second ingestion for shared `span_id`s.
- [ ] A payload whose `spans` list happens to list a child span before its parent still resolves `parent_span_id` correctly — confirmed by a test fixture constructed with that ordering.
- [ ] No `id_mappings`-style table exists in the schema — confirmed by inspection.
- [ ] No API response at any layer exposes `spans.id` (the internal surrogate integer) — only the external `span_id` string appears.

---

## Task 5 — Idempotent repeated ingestion

### Scope framing

A good portion of this is already answered by Tasks 1 and 4: natural/composite PKs were specifically chosen so `ON CONFLICT ... DO UPDATE` is the upsert mechanism everywhere, and Task 4's `RETURNING`-based span resolution is itself idempotent.

### Pure upsert, never delete

- Every snapshot is a full restatement (Phase 1 Task 7), so a later snapshot for a known `run_id` is always a superset (or equal set) of what came before. There is never a legitimate case where re-ingestion needs to remove a previously-stored span/tool_call/llm_call — ingestion is upsert-only, no reconciliation/delete pass.

### Last-write-wins, no conflict detection

- If the same child ID appears with different values across two snapshots, the later one simply overwrites — already the semantics Phase 1 Task 7 locked ("a new snapshot... is authoritative and superseding"). No value-drift warning/flagging logic.

### No concurrency/locking machinery

- No advisory locks, no retry-on-conflict wrapping — relies on Postgres's own per-statement atomicity, consistent with Phase 6's rejection of a dedicated Postgres sequence for `work_order_id` on the same "single-request-at-a-time, debug-scale" reasoning.

### No short-circuit optimization

- No hash-comparison or "is this payload identical to what's stored" check to skip writing. At fixture scale, always performing the upsert (even when the result is unchanged) costs nothing worth guarding against.

### Upsert style — per-row loop, not bulk multi-row

- Each span/tool_call/llm_call gets its own individual `INSERT ... ON CONFLICT ... DO UPDATE` statement, iterated in application code — not one bulk multi-row `INSERT ... VALUES (...), (...), ...` statement per table.
- Rationale: the fixture corpus has only a handful of spans/tool_calls per run, so the round-trip cost difference between per-row and bulk is immaterial; per-row keeps each write independently traceable (a failing row's error points at exactly that row) and matches the object-per-row `session.add()` style Project 1's own repository layer already uses. This is a deliberate inspectability-over-raw-efficiency choice, consistent with this project's repeated posture at this scale.

### Test / Validation

- [ ] Ingesting a known fixture, then re-ingesting the identical payload, results in the exact same row counts in `agent_runs`/`spans`/`tool_calls`/`llm_calls` — no duplication.
- [ ] Re-ingesting an identical payload updates `agent_runs.updated_at` (or leaves domain fields unchanged) without altering any child row's identity (`spans.id` values are stable across the re-ingestion).
- [ ] No child row is ever deleted as part of ingestion — confirmed by a test that ingests a fixture, manually adds an extra unrelated row for the same `run_id`, re-ingests, and confirms the extra row survives (this test also documents the "growing set only" invariant, distinct from an actual producer behavior).
- [ ] Persistence code performs one upsert statement per row (not a single bulk multi-row statement) — confirmed by code inspection / query log during a test ingestion.

---

## Task 6 — Lifecycle-aware upsert for HITL pending → approved/rejected/completed

### Full, unconditional column overwrite — no `COALESCE`

- `agent_runs`' upsert is `DO UPDATE SET <every column> = excluded.<every column>`, unconditionally, for every field — no "preserve old value if new is null" logic anywhere. This is what correctly implements the pending→approved transition: `hitl_pending_action` legitimately goes from a populated JSONB blob to `NULL` once approved (per Phase 1's required/optional matrix — it's only required while `state == PENDING`), and `completed_at`/`final_result` go from `NULL` to populated. A `COALESCE`-style "keep old value if null" pattern would silently preserve the stale `pending_action` forever and get this exactly backwards.

### No separate HITL audit/history table

- Already explicitly ruled out by this phase's own key design constraints ("do not introduce... a human-approval table"). `agent_runs` stores only the current state; there is no event-sourced log of "pending at T1, approved at T2" beyond what each snapshot's own `hitl.requested_at`/`hitl.decided_at` fields carry.

### Trust the producer for `resume_count` and all other fields

- No validation that `resume_count` increments correctly, or of any other producer-supplied field — store as sent.

### Terminal-state guard (added, not silently skipped)

- Before upserting `agent_runs`, the ingestion service reads the row's current `hitl_state` (if the run already exists); if it's already `APPROVED` or `REJECTED` and the incoming payload's `hitl.state` is `PENDING`, the request is rejected with an explicit, loud error rather than silently overwritten or silently no-op'd.
- Rationale for treating this differently from Task 4's "trust the producer" posture: the stakes are materially different. Task 4's scenario (a snapshot silently missing a previously-known child) is an inspectability nuisance; this scenario (an already-submitted, real-world work order appearing "pending" again in the observability layer) is a materially misleading state for anyone reading the Query API/dashboard later. The guard is cheap (one extra `SELECT` plus a conditional) and earns its keep given the asymmetry in consequences.

### Test / Validation

- [ ] Ingesting `hitl_pending` then `hitl_approved` results in exactly one `agent_runs` row whose `hitl_state`, `status`, `event_type`, `completed_at`, `final_result_output`, and `hitl_pending_action` all reflect the *approved* snapshot's values — with `hitl_pending_action` specifically now `NULL`.
- [ ] A test forcibly re-posting a `PENDING`-state payload for a `run_id` whose stored row is already `APPROVED` is rejected with an explicit error, and the stored row's `hitl_state` remains unchanged (still `APPROVED`).
- [ ] No table in the schema records a history of `hitl_state` transitions — confirmed by schema inspection.
- [ ] The `hitl_approved` fixture's new `submit_work_order` tool call appears as a new row in `tool_calls` after ingestion, without disturbing any row carried over from `hitl_pending`.

---

## Task 7 — Derived/denormalized run usage totals

### Project 2 recomputes totals itself; the producer's `usage` field is not trusted for storage

- Phase 1's `ExtendedRunEvent.usage: UsageSummary` is producer-computed and part of the validated contract, but it is **not** what lands in `agent_runs.usage_*`. Those columns are Project 2's own derivation from the persisted `tool_calls`/`llm_calls` rows.
- Rationale: this phase's own task wording — "keeping granular calls as the source of truth" — only means something if Project 2 independently derives the totals; blindly copying the producer's self-reported summary would let stored totals silently drift from what's actually persisted if Project 1's own summation ever had a bug, with nothing to catch it. The plan's own test bullet ("verify stored usage totals match granular calls") only has teeth under this reading.

### Derivation source — from the database, post-write

- After Task 5/6's upserts commit within the ingestion transaction, one `UPDATE agent_runs SET usage_total_... = (SELECT ... FROM tool_calls/llm_calls WHERE run_id = ...) WHERE run_id = :run_id` runs using correlated aggregate subqueries — still inside the same atomic transaction.
- Rejected alternative: deriving totals from the in-memory validated `ExtendedRunEvent` object before writing (avoids an extra query round trip). Rejected because deriving from the database is a genuine self-consistency check — immune to any subtle bug elsewhere in the upsert path that might silently drop a row before it reaches the DB — whereas deriving from memory merely assumes memory matches disk. The extra round trip is immaterial at fixture scale.

### Concrete formulas

- `usage_total_tool_calls` = `COUNT(*)` over `tool_calls WHERE run_id = :run_id`.
- `usage_total_llm_calls` = `COUNT(*)` over `llm_calls WHERE run_id = :run_id`.
- `usage_total_tokens` = `SUM(llm_calls.total_tokens)` — using the column already present on each `LLMCall`, not re-deriving from `prompt_tokens + completion_tokens`.
- `usage_total_retries` = `SUM(tool_calls.retry_count)` only — `LLMCall` has no `retry_count` field in the Phase 1 contract.
- `usage_total_estimated_cost_usd` = `SUM(llm_calls.estimated_cost_usd)`.
- All sums `COALESCE`d to `0` for a run with zero tool/LLM calls (e.g. a run failing before any tool call), so the columns are never `NULL`.

### Scope boundary

- Latency percentiles (avg/p95) are explicitly **not** part of this task's denormalized totals — those remain a query-time aggregate computed by Phase 3's analytics endpoints directly over granular rows, since percentiles are typically meaningful across runs, not per single run.

### Test / Validation

- [ ] For every canonical fixture, `agent_runs.usage_total_*` after ingestion exactly equals the corresponding `COUNT`/`SUM` computed directly over that run's `tool_calls`/`llm_calls` rows — confirmed by a test recomputing the aggregates independently and diffing against the stored values.
- [ ] `agent_runs.usage_total_*` values do **not** equal the ingested payload's own `usage: UsageSummary` field when a test deliberately constructs a fixture where the producer-reported summary is wrong — confirming derivation, not passthrough.
- [ ] `tool_failure` (a fixture with zero LLM calls reaching synthesis, per Phase 1) has `usage_total_tokens`/`usage_total_estimated_cost_usd` computed correctly (not `NULL`) from whatever LLM calls did occur.
- [ ] No column on `agent_runs` stores an average or percentile latency value.

---

## Task 8 — Minimal indexes

### Scope framing

Guided directly by the task's own word "minimal," mapped against Phase 3's already-known query shapes (run listing filters, run detail, and the three analytics endpoints) rather than speculative future patterns.

### `agent_runs`

- `run_id` already indexed via PK. Add single-column indexes on `status`, `scenario_id`, `agent_version`, `started_at`.
- Deliberately no hand-tuned composite index (e.g. `(status, started_at)`) for a guessed filter combination — Postgres's bitmap index combination handles the various combinations `GET /v1/runs` might request well enough at this scale; building a composite now would index for a specific query shape Phase 3 hasn't locked yet.

### `spans`

- `(run_id, span_id)` already indexed via Task 1's unique constraint. Add an index on `parent_span_id` (self-referential FK; Postgres does not auto-index FK columns) to support reconstructing the span tree for `GET /v1/runs/{run_id}`.

### `tool_calls`

- `(run_id, tool_call_id)` already indexed as the natural PK. Add an index on `span_id` (FK). Add a composite index on `(tool_name, status)` — tailored specifically, unlike the free-form run-listing filters, because `GET /v1/analytics/tools`' per-tool failure-rate query is a fixed, known shape (group by tool, count by status) that Phase 3's own constraints explicitly frame as *not* open-ended.

### `llm_calls`

- `(run_id, llm_call_id)` already indexed as the natural PK. Add an index on `span_id` (FK) and on `model`.
- The `model` index is worth flagging explicitly: `GET /v1/runs`' "filter by model" isn't a column on `agent_runs` — a run's model lives on its `llm_calls` rows, not the run itself (no "primary model" was denormalized onto `agent_runs` in Task 1/7). That filter is a semi-join/`EXISTS` against `llm_calls`, and this index makes both that and `GET /v1/analytics/usage`'s model-grouping efficient.

### `evaluation_results`

- `UNIQUE(run_id, evaluator_name, evaluator_version, regression_run_id)` already gives a free leading-column index on `run_id`, sufficient for "evaluator lookup for a run." No index added for `evaluator_name`-level cross-run aggregation — that query shape belongs to Phase 5, not yet defined.

### `run_failures`

- `run_id` is already the PK. No additional index — failure-category analytics indexing deferred until whichever phase actually builds that endpoint and defines its real access pattern.

### Test / Validation

- [ ] `EXPLAIN` on a `GET /v1/runs` query filtered by `status` (or `scenario_id`, `agent_version`, or a `started_at` range) shows the relevant single-column index being used, not a sequential scan, once the table has enough rows for the planner to prefer it.
- [ ] `EXPLAIN` on the per-tool failure-rate aggregation query shows the `(tool_name, status)` composite index in use.
- [ ] `EXPLAIN` on a "runs using model X" semi-join query shows the `llm_calls.model` index in use.
- [ ] No index exists on `run_failures.primary_category`/`secondary_category`, or on `evaluation_results.evaluator_name` alone — confirmed by schema inspection, consistent with the deferral decisions above.

---

## Success Criteria

- [ ] All eight canonical Phase 1 fixtures can be validated, normalized into PostgreSQL, and reconstructed exactly — matching run/span/tool/LLM row counts and relationships — closing the plan's own "ingest a known fixture" test bullet (Tasks 1–4).
- [ ] Re-ingesting an identical snapshot, or progressing a run through HITL pending → approved/rejected, never duplicates rows; the transition is handled by a single evolving `agent_runs` row via full unconditional overwrite, with an explicit guard against backward lifecycle regressions (Tasks 5, 6).
- [ ] Granular `tool_calls`/`llm_calls` rows remain the sole source of truth for usage/cost/token accounting — `agent_runs`' denormalized totals are independently derived from persisted rows via a post-write database aggregate, never copied from the producer's self-reported `usage` field (Task 7).
- [ ] The schema reserves exactly one forward-compatible seam for Phase 7 (a bare, unconstrained `regression_run_id` column and an append-grain `evaluation_results` table) without speculatively building Phase 7's own undesigned `regression_runs` table (Task 1).
- [ ] Indexing is scoped exactly to Phase 3's already-known query shapes — run-listing filters, run-detail reconstruction, and the three fixed analytics endpoints — with no speculative indexing ahead of endpoints or taxonomies (evaluator/failure) that don't exist yet (Task 8).
- [ ] Persistence logic is independently testable from FastAPI, and both validation and DB failures surface loudly (FastAPI's default `422`/an uncaught `500`) rather than being silently swallowed, matching this phase's "ingestion failure is a Project 2 concern" framing (Task 3).
- [ ] Two column-type distinctions are drawn deliberately rather than defaulted: relational vs. JSONB (Task 2), and CHECK-constrained vs. plain-text vocabularies, the latter reserved for taxonomies Phase 4/5 haven't defined yet (Task 1).

## Status

All eight Phase 2 tasks are locked. Phase 2 planning is complete. Next: proceed to implementation, or move on to Phase 3 (Core Query API & Operational Analytics) planning discussion.