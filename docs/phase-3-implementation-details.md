# Phase 3 (Core Query API & Operational Analytics) — Implementation Decisions

Captured from planning discussion, 2026-08-28. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 3 and building on the Phase 0 (Walking Skeleton), Phase 1 (Telemetry Contract & Debug Fixtures), and Phase 2 (Ingestion & Persistence) decisions without contradicting any of them. Decisions are locked task-by-task, following the Phase 3 task list from the implementation plan:

1. Implement `GET /v1/runs` with pagination and agreed filters. **(locked)**
2. Implement `GET /v1/runs/{run_id}` returning run summary, request/final output, spans, ordered tool calls, LLM calls, HITL state, usage, and runtime error information. **(locked)**
3. Implement `GET /v1/analytics/overview` for runtime success, average/p95 latency, aggregate token usage/cost, and run counts. **(locked)**
4. Implement `GET /v1/analytics/tools` for per-tool call volume, failure rate, average latency, and p95 latency. **(locked)**
5. Implement `GET /v1/analytics/usage` with agent-side token/cost aggregates and model grouping. **(locked)**
6. Keep API response models explicit and stable enough for the future dashboard. **(locked)**

All six Phase 3 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0–2 were planned. See Success Criteria and Status at the bottom.

---

## Task 1 — `GET /v1/runs` with pagination and filters

### Pagination & counting strategy (the central decision for this task)

- **Offset/limit pagination with a `total` count** — `?limit=&offset=`, response envelope `{"items": [...], "total": N, "limit": L, "offset": O}`. `total` is computed via a `COUNT(*)` (or `COUNT(*) OVER()`) matching the same filters as the page query.
- Rejected alternative: cursor/keyset pagination (`?limit=&cursor=`, response with `next_cursor`/`has_more`, no total). Rejected because its main benefit — stable, efficient pagination at arbitrary depth — targets a scale this project will never reach (fixture/portfolio-scale run counts, not millions of rows), while its costs (no total count for a dashboard summary, no jump-to-page, cursor encoding/decoding ceremony) are real and immediate. Matches this project's repeated "debug-scale, prefer simple over premature infrastructure" posture (e.g. Phase 2 rejecting a dedicated Postgres sequence for the same reason class).

### Filters and sort

- Filter set for this phase: `status` (enum, exact match, single value only — no repeated-param OR-filter in v1), `scenario_id` (exact match), `agent_version` (exact match), `model` (exact match, resolved via `EXISTS` against `llm_calls`, per the index Phase 2 Task 8 already built for this purpose), `started_after`/`started_before` (ISO 8601 datetimes, filtering `agent_runs.started_at`). All filters combine with AND.
- No `evaluation`/`failure` filters yet — those columns/tables don't exist until Phase 4/5. The query-param filter model is additive by construction, so those land later as new optional params, not a redesign.
- **Fixed sort order: `started_at DESC`.** No generic `sort_by`/`sort_order` param — an arbitrary sort-field param is a step toward the "generic SQL/search builder" the plan explicitly rules out.

### Response shape and limits

- **List item is a lightweight `RunSummary`**, distinct from Task 2's full detail: `run_id`, `scenario_id`, `agent_name`, `agent_version`, `prompt_version`, `environment`, `status`, `event_type`, `hitl_state`, `started_at`, `completed_at`, `execution_latency_ms`, `wall_clock_duration_ms`, `usage_total_tokens`, `usage_total_estimated_cost_usd`. No spans/tool_calls/llm_calls/payloads — the list is for discovery/scanning, the detail endpoint is for inspection.
- **Default `limit=20`, max `limit=100`**, enforced via the Pydantic query model's field constraints — an over-limit request is rejected (`422`), not silently clamped.
- Invalid filter values (e.g. an out-of-vocabulary `status`) fail with FastAPI/Pydantic's default `422`, same posture as Phase 2 Task 3's ingestion validation — no custom error envelope.

### Test / Validation

- [ ] `GET /v1/runs` with no params returns the most recent `limit=20` runs ordered strictly by `started_at DESC`, with `total` equal to the full unfiltered row count.
- [ ] Each of `status`, `scenario_id`, `agent_version`, `model`, `started_after`/`started_before` independently narrows results to exactly the matching rows against a seeded fixture set; combining two filters applies AND semantics.
- [ ] The `model` filter correctly matches runs via their `llm_calls.model` rows, not a column on `agent_runs` itself.
- [ ] A `limit` above `100` or an invalid `status` value returns `422`, not a clamped/silently-corrected result.
- [ ] `RunSummary` list items contain no `spans`/`tool_calls`/`llm_calls`/payload fields — confirmed by response-shape inspection.
- [ ] `total` reflects the filtered count, not the unfiltered table size, when filters are applied.

---

## Task 2 — `GET /v1/runs/{run_id}`

### Response model: dedicated, not reused from the telemetry contract (the central decision for this task)

- **A dedicated `RunDetailResponse`** (plus nested `SpanResponse`/`ToolCallResponse`/`LLMCallResponse`/`HITLResponse`/`UsageResponse`), owned by the API layer, structurally similar to but independent from the `telemetry.v1` contract models.
- Rejected alternative: reusing/extending `ExtendedRunEvent` directly as the response shape. Rejected because `ExtendedRunEvent` is the producer-facing wire contract, frozen by Phase 1 ("stable after Phase 1 unless a blocking inconsistency is discovered"); reusing it here would couple ingestion and query concerns, and would break down as soon as Phase 5 needs to add evaluation/failure fields to the run-detail response — fields that have no business on a producer-emitted document. The locked technical baseline table already lists "Telemetry contract" and "Query/API boundary" as two distinct rows, not one.

### Trajectory representation: flat ordered lists, not a nested tree

- **`spans`, `tool_calls`, `llm_calls` are returned as flat lists, ordered by `sequence`**, each item carrying its own ID and (for tool_calls/llm_calls) `span_id` FK — mirroring the shape `ExtendedRunEvent` already ingests, not one array nested inside another.
- Rejected alternative: a server-built nested span tree (tool_calls/llm_calls nested under their owning span). Rejected because it adds real implementation complexity (recursive assembly, multiple-roots handling) for a consumer (Phase 8's dashboard) that doesn't exist yet and isn't guaranteed to want a tree over a flat timeline — the kind of building-ahead-of-need this project has consistently avoided. A flat, sequence-ordered list also directly satisfies the plan's own test bullet ("verify ordering of trajectory data is deterministic") in the simplest possible way; a tree has no single global order to assert against.
- Each item explicitly carries its own `sequence` value in the response (not just implied by array position), making the ordering guarantee independently checkable.

### Straightforward details

- Unknown `run_id` → `404`, never a `200` with an empty body.
- **No internal surrogate IDs exposed** — only external `span_id`/`tool_call_id`/`llm_call_id` strings appear anywhere in the response; `spans.id` stays server-internal. Carries forward Phase 2 Task 4's rule verbatim.
- HITL block (`required`, `state`, `checkpoint_id`, `decision`, `requested_at`, `decided_at`, `pending_action`) reads straight off `agent_runs`' flattened HITL columns (Phase 2 Task 1).
- Usage block reads the four pre-aggregated `usage_total_*` columns (Phase 2 Task 7) — not recomputed at query time.
- Runtime error block is nullable, populated only when `status` is `TOOL_ERROR`/`RUNTIME_ERROR`.
- Request/response fields: `raw_input` (JSONB passthrough), `normalized_input` (nullable), `final_result` (`output` + `source_references`, nullable pre-completion).

### Test / Validation

- [ ] `GET /v1/runs/{run_id}` for an unknown ID returns `404`.
- [ ] For a known run, `spans`/`tool_calls`/`llm_calls` are returned as flat, `sequence`-ordered lists, not nested under one another — confirmed by response-shape inspection.
- [ ] No response field anywhere exposes `spans.id` (the internal surrogate integer) — only `span_id` strings appear.
- [ ] The response's HITL/usage/runtime_error blocks match the corresponding `agent_runs` columns exactly for a seeded fixture (e.g. `hitl_pending`/`hitl_approved`).
- [ ] `RunDetailResponse` is a model class distinct from `ExtendedRunEvent` — confirmed by code inspection (different module, different class).
- [ ] Re-ingesting `hitl_pending` then `hitl_approved` and re-fetching the detail endpoint shows the carried-over span/tool_call/llm_call IDs unchanged and the new `submit_work_order` tool call appended, still in `sequence` order.

---

## Task 3 — `GET /v1/analytics/overview`

### Time-range filter (fork 1)

- **Optional `started_after`/`started_before`**, reusing the same param shape as Task 1, defaulting to all-time when omitted.
- Rejected alternative: a fixed all-time-only aggregate with no filter. Rejected because an all-time-only number gets less useful the longer the project runs, and this is exactly the kind of "how are things looking recently" question an overview screen exists to answer — while still not qualifying as a "generic search builder" since it's just two already-established params applied to one more query.

### Runtime-success-rate denominator (fork 2)

- **Denominator = only terminal runs (`event_type = RUN_FINAL`).** `AWAITING_APPROVAL` runs are excluded from the success-rate ratio itself but still counted in the overall `run_counts` breakdown.
- Rejected alternative: including all in-scope runs (pending included) in the denominator, counting a pending run as "not success" until resolved. Rejected as actively misleading — a run awaiting human approval hasn't succeeded or failed, and counting it against the rate would drag the metric down for reasons unrelated to agent behavior, self-correcting only once resolved. Matches this project's established discipline of keeping adjacent-but-different concepts distinct (runtime status vs. evaluation status; here, concluded vs. pending).

### Straightforward details

- **Latency stat uses `execution_latency_ms`, not `wall_clock_duration_ms`.** A HITL run's wall-clock time includes indefinite human-approval wait time, which would badly skew p95 with outliers unrelated to actual agent performance. `wall_clock_duration_ms` remains available per-run on the Task 2 detail response; adding it here later is a non-breaking addition if ever wanted.
- `avg`/`p95` computed via Postgres `AVG()`/`PERCENTILE_CONT(0.95) WITHIN GROUP`, over `agent_runs.execution_latency_ms WHERE execution_latency_ms IS NOT NULL` — which naturally excludes pending runs without extra filtering logic (the field is null while pending, per Phase 1's required/optional matrix).
- Aggregate token/cost = `SUM(usage_total_tokens)` / `SUM(usage_total_estimated_cost_usd)` over the in-scope run set — reads Phase 2 Task 7's denormalized columns, not a recompute over `llm_calls`.
- `run_counts`: total in scope, plus a breakdown by `status` (all four `RunStatus` values, including `AWAITING_APPROVAL`).
- No `scenario_id`/`agent_version`/`model` filters on this endpoint — those stay exclusive to Task 1 and the dedicated Tasks 4/5 groupings (and the future Phase 7 `/v1/analytics/scenarios`), keeping this endpoint a single named aggregate rather than a second general-purpose filter surface.
- No caching/materialized view — a direct aggregate query per call, per the plan's explicit "prefer simple indexed queries over premature caching" constraint.
- Response is a single flat object, not a paginated list.

### Test / Validation

- [ ] With no time-range params, the endpoint aggregates over all runs; with `started_after`/`started_before` set, results are scoped to exactly the matching runs against a seeded fixture set.
- [ ] A run in `AWAITING_APPROVAL` (e.g. `hitl_pending`) is excluded from the success-rate ratio's denominator but appears in `run_counts`' total and status breakdown.
- [ ] `avg_latency_ms`/`p95_latency_ms` are computed from `execution_latency_ms`, not `wall_clock_duration_ms` — confirmed by a test fixture where the two values deliberately differ.
- [ ] `usage_total_tokens`/`usage_total_estimated_cost_usd` aggregates match `SUM()` over the seeded fixtures' `agent_runs.usage_total_*` columns exactly.
- [ ] The endpoint accepts no `scenario_id`/`agent_version`/`model` query params — confirmed by code inspection.

---

## Task 4 — `GET /v1/analytics/tools`

### Straightforward details (no fork — shape is largely inherited from Phase 2 Task 8's `(tool_name, status)` index, built specifically for this query)

- Same optional `started_after`/`started_before` time-range filter as Task 3, for consistency. No `tool_name` scoping param — the endpoint's job is comparing tools against each other, so it always returns every tool present in the (time-scoped) data.
- **No canonical tool list.** `ToolCall.tool_name` is free text, not an enum (same reasoning Phase 1 applied to `Span.name` — Project 2 stays agent-agnostic). A tool with zero calls in scope simply doesn't appear; there's no fixed vocabulary to backfill a zero-row entry against.
- **Failure rate** = `(FAILURE + ERROR count) / total count` per `tool_name`, using the shared `ExecutionStatus` enum. Raw per-status counts (`success_count`, `failure_count`, `error_count`) are included alongside the scalar rate, so no granularity is lost by combining FAILURE/ERROR into one rate.
- Latency: `ToolCall.latency_ms` directly (single field, no execution-vs-wall-clock ambiguity here) — `avg`/`p95` via the same Postgres aggregate pattern as Task 3, `WHERE latency_ms IS NOT NULL`.
- Sort: `call_count DESC`, `tool_name ASC` tiebreak — busiest tools first, matching the task's own field ordering. No server-side `sort_by` param, consistent with Task 1.
- No pagination — the tool vocabulary is small by construction (Phase 1's fixture corpus names exactly seven real tools).
- Response: a flat list of `{tool_name, call_count, success_count, failure_count, error_count, failure_rate, avg_latency_ms, p95_latency_ms}` objects.

### Test / Validation

- [ ] Each tool present in a seeded fixture set appears exactly once, with `call_count` equal to its total row count in `tool_calls`.
- [ ] `failure_rate` for a tool with a mix of `SUCCESS`/`FAILURE`/`ERROR` calls equals `(failure_count + error_count) / call_count` exactly.
- [ ] Results are ordered by `call_count DESC`, `tool_name ASC` — confirmed against a fixture set with known, distinct call counts.
- [ ] A tool name absent from the (time-scoped) data does not appear in the response — confirmed no zero-row placeholder is produced.
- [ ] `avg_latency_ms`/`p95_latency_ms` are computed from `tool_calls.latency_ms` and match manual recomputation over the same rows.

---

## Task 5 — `GET /v1/analytics/usage`

### Grouping scope: model breakdown alone, or model *and* call_type breakdown (the central decision for this task)

- **Both**: overall totals, a per-`(provider, model)` breakdown, and a per-`call_type` breakdown, each computed as a dynamic `GROUP BY` over whatever distinct values are actually present in the data — not a hardcoded enumeration.
- Rejected narrower alternative: model grouping only, exactly matching the task's literal wording. Rejected in favor of also including `call_type` because it directly serves the project's stated interest in quality/latency/cost trade-off analysis, and it's cheap — `call_type` is already a small closed vocabulary sitting on the same `llm_calls` rows this endpoint already scans, with no new joins or tables required.
- **`call_type`'s vocabulary is not redefined or extended here.** `LLMCallType` (`{INTERPRETATION, EVIDENCE_GATHERING, SYNTHESIS}`) was already locked as a closed enum in Phase 1 Task 5, as part of the frozen `telemetry.v1` contract, which the plan's own cross-phase rule treats as stable after Phase 1. This endpoint only consumes it — the `GROUP BY` is dynamic over present values, so if a hypothetical future `v2` telemetry package ever added a fourth call type, this endpoint would surface it automatically with zero code change.

### Agent-side scope boundary (locked explicitly, not just implied)

- This endpoint reads **exclusively from `llm_calls`** — the observed agent's own model calls. It never includes judge/evaluation LLM cost, which doesn't exist yet but lands in Phase 6 on its own separate accounting. Locking this boundary now matters because "agent cost and evaluation cost remain distinct" is a cross-phase rule the plan states explicitly (and reiterates for Phase 6) — this endpoint is the agent-cost half; Phase 6 gets its own accounting rather than this one growing a `source` filter later.

### Straightforward details

- Same optional `started_after`/`started_before` filter as Tasks 3/4, no other filters.
- Group by `(provider, model)` as a pair, not `model` alone — cheap correctness margin against two providers ever reporting the same model string with different pricing; costs nothing today since the fixture corpus has only one combination (`anthropic`/`claude-sonnet-4-6`).
- **No latency stats on this endpoint** — Task 5's own wording asks for token/cost and model grouping only; LLM-call latency isn't named, and tool latency already has a clear home in Task 4.
- No canonical model list — same reasoning as Task 4's tool list.
- Per-model and per-call_type breakdowns both sorted by `total_estimated_cost_usd DESC` — cost is the headline concern for a usage/cost endpoint.
- Both the overall totals and each breakdown group report `prompt_tokens`, `completion_tokens`, and `total_tokens` separately, matching `LLMCall`'s own granularity.
- No derived per-call averages (e.g. `avg_cost_per_call`) — raw sums and counts only, trivially derivable client-side.
- No pagination — same small-vocabulary reasoning as Task 4.

### Test / Validation

- [ ] The response includes overall totals plus both a `by_model` and a `by_call_type` breakdown, each summing back to the overall totals for a seeded fixture set.
- [ ] `by_model` groups by `(provider, model)` pairs, not `model` alone — confirmed by a test fixture with two providers sharing a model string.
- [ ] `by_call_type` reflects exactly the `LLMCallType` values present in the scoped data — confirmed no hardcoded enumeration of all three values appears when a fixture subset only uses two.
- [ ] No response field on this endpoint includes any latency statistic — confirmed by response-shape inspection.
- [ ] No judge/evaluation-cost data appears in this endpoint's output — confirmed by code inspection (query scoped to `llm_calls` only).
- [ ] `prompt_tokens`/`completion_tokens`/`total_tokens` are each independently correct (not just `total_tokens` alone) at both the overall and per-group level.

---

## Task 6 — Keep API response models explicit and stable enough for the future dashboard

### Module organization

- All response models from Tasks 1–5 live in one explicit, dedicated module — `src/obs_platform/api/v1/schemas.py` — separate from `telemetry.v1`, making Task 2's fork resolution (dedicated response layer, not a reused wire contract) concrete. Mirrors Phase 1's "few tightly-coupled types, one file" precedent.
- Routes stay organized per Phase 0's established pattern (`routes/runs.py`, `routes/analytics.py` — one `APIRouter` per concern), importing shapes from the shared `schemas.py`.
- The existing `/v1` URL prefix (Phase 0) **is** this contract's versioning seam — no new versioning machinery is introduced; a breaking API reshape, if ever needed, gets a sibling `api/v2/` module, the same pattern as `telemetry/v1` → a hypothetical `v2`.

### Explicit `response_model=` everywhere

- No route returns a bare `dict`. Every endpoint from Tasks 1–5 declares `response_model=` explicitly — this makes the OpenAPI schema generated (not hand-maintained prose) and makes FastAPI validate/filter the outgoing payload, so a route accidentally leaking an internal field fails loudly rather than shipping silently.

### Naming convention

- `RunSummary` / `RunListResponse` (Task 1), `RunDetailResponse` + `SpanResponse`/`ToolCallResponse`/`LLMCallResponse`/`HITLResponse`/`UsageResponse` (Task 2), `OverviewAnalyticsResponse` (Task 3), `ToolAnalyticsResponse` + `ToolStats` (Task 4), `UsageAnalyticsResponse` + `ModelUsageBreakdown`/`CallTypeUsageBreakdown` (Task 5).

### Enum reuse, shape independence

- Status-shaped fields (`status`, `event_type`, `hitl_state`, `call_type`, etc.) type against the **same** `telemetry.v1` `StrEnum`s (`RunStatus`, `ExecutionStatus`, `HITLState`, `LLMCallType`) rather than a second parallel copy — narrower reuse than Task 2's rejected "reuse the whole contract model," since enums are pure closed vocabulary, not shape. The response *shapes* stay independently defined, per Task 2.

### Construction pattern

- A small shared `APIResponseModel` base sets `model_config = ConfigDict(from_attributes=True)` once, so response models build directly from SQLAlchemy row objects without manual field-by-field unpacking at each route.

### Additive-only discipline going forward

- Once Phase 3 ships, Phase 4/5's known upcoming extensions (`evaluation_summary`, `failure` fields on `RunSummary`/`RunDetailResponse`/`OverviewAnalyticsResponse`) must be additive new optional fields — never a rename, removal, or restructuring of what's defined here. Stated explicitly now so Phase 4/5 planning inherits it as a constraint.

### Documentation quality

- Every conditionally-present field (nullable-until-some-state, e.g. `runtime_error`, `pending_action`) carries a `Field(description=...)` stating when it's populated. Every route gets a one-line `summary` — this is how a future Phase 8 dashboard implementer discovers the contract via `/docs`.

### Explicitly not built now

- No OpenAPI schema-diffing in CI, no contract/snapshot testing. There's no real consumer yet (Phase 8 doesn't exist), so guarding against a break with no current victim is premature machinery this project has consistently declined to build elsewhere. The additive-only rule is the actual safeguard for now; automated enforcement can wait until Phase 8 gives it a real reason to exist.

### Test / Validation

- [ ] Every route defined in Tasks 1–5 declares an explicit `response_model=` — confirmed by code inspection; no route returns a bare `dict`.
- [ ] All response model classes live under `src/obs_platform/api/v1/schemas.py`, none under `telemetry.v1` — confirmed by import inspection.
- [ ] `RunSummary`/`RunDetailResponse`/analytics response models' status-shaped fields import their enum types from `telemetry.v1.enums`, not a locally redefined copy — confirmed by code inspection.
- [ ] The generated OpenAPI schema (`/openapi.json`) includes a non-empty `description` for every conditionally-nullable field identified above.
- [ ] No CI step performs OpenAPI schema diffing or contract-snapshot testing — confirmed by inspecting `.github/workflows/*.yml`, consistent with the explicit-deferral decision.

---

## Success Criteria

- [ ] A reviewer can ingest the Phase 1 fixture corpus and, using only `GET /v1/runs` and `GET /v1/runs/{run_id}`, reconstruct the complete behavior of any run — request, trajectory, tool/LLM activity, HITL state, usage, and runtime error — exactly as it was ingested (Tasks 1, 2).
- [ ] The three analytics endpoints answer the phase's own framing question — "what happened, which tools/LLM calls executed, and how much latency/token/cost was incurred" — using aggregates verifiable against known fixture values, with runtime success kept structurally distinct from any future behavioral evaluation success (Tasks 3, 4, 5).
- [ ] Every filter, sort, and pagination choice made across Tasks 1, 3, 4, and 5 stays within the plan's explicit boundary against generic SQL/search builders or arbitrary analytics endpoints — each endpoint answers one fixed, named question, not an open-ended query surface.
- [ ] The dashboard-facing contract (Task 2's dedicated response models, Task 6's explicit `response_model=`/naming/documentation discipline) is structurally ready for Phase 8 to consume without redesign, and for Phase 4/5 to extend additively without breaking it.
- [ ] No new persistence, caching, or queue infrastructure was introduced to answer any of these five endpoints — every one is a direct indexed query against the Phase 2 schema, consistent with "prefer simple indexed queries over premature caching/materialized-view infrastructure."

## Status

All six Phase 3 tasks are locked. Phase 3 planning is complete. Next: proceed to implementation, or move on to Phase 4 (Deterministic Evaluation) planning discussion.