# Phase 5 (Failure Classification & Evaluation API) — Implementation Decisions

Captured from planning discussion, 2026-08-30. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 5 and building on the Phase 0 (Walking Skeleton), Phase 1 (Telemetry Contract & Debug Fixtures), Phase 2 (Ingestion & Persistence), Phase 3 (Core Query API & Operational Analytics), and Phase 4 (Deterministic Evaluation) decisions without contradicting any of them — with two explicit, reasoned amendments to Phase 4 Task 5 and Phase 2 Task 1 noted below. Decisions are locked task-by-task, following the Phase 5 task list from the implementation plan:

1. Implement `POST /v1/runs/{run_id}/evaluate`. **(locked)**
2. Define evaluator execution status independently from pass/fail: pending, running, completed, failed, skipped. **(locked)**
3. Implement overall PASS / FAIL / INCOMPLETE summary rules without a weighted global quality score. **(locked)**
4. Implement the agreed failure taxonomy and deterministic severity mapping. **(locked, amends Phase 4 Task 5)**
5. Implement `FailureClassifier` producing one primary failure type plus optional secondary types and maximum severity. **(locked)**
6. Persist `RunFailure` separately from `EvaluationResult`. **(locked, amends Phase 2 Task 1's schema)**
7. Add failure/evaluation information to run list and run-detail API responses. **(locked)**
8. Implement `GET /v1/analytics/failures`. **(locked)**
9. Extend `GET /v1/analytics/overview` with behavioral evaluation pass rate while preserving runtime success separately. **(locked)**

All nine Phase 5 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0–4 were planned. See Success Criteria and Status at the bottom.

---

## Task 1 — Implement `POST /v1/runs/{run_id}/evaluate`

### Already decided upstream (not re-opened here)

- **Synchronous, in-request execution — no background worker.** Design doc §7.1 ("Evaluation Trigger") already locked this: an explicit evaluation trigger after ingestion, no Celery/Redis; background execution may be added later without changing evaluator contracts.
- **Evaluator scope = the entire current registry, no selection param.** Runs all of `DETERMINISTIC_EVALUATORS` (Phase 4 Task 1) every time; no `evaluator_names`/`evaluator_types` filter — consistent with this project's repeated rejection of speculative config surfaces. Phase 6's LLM-based evaluators join the same pipeline additively later.
- **No request body.** `run_id` from the path is the only input. `regression_run_id` stays `None` for every result this endpoint produces (Phase 7's regression runner is a distinct orchestration path).
- **Unknown `run_id` → `404`.**
- **No lifecycle-state gating** — callable on a run in any `status`/`event_type`/`hitl_state`, including `AWAITING_APPROVAL`. All five deterministic evaluators already handle non-terminal/non-golden runs gracefully (`not_applicable` or universal application); restricting evaluation to terminal-only runs would be an invented rule with no source in the plan or design doc.

### Per-evaluator failure isolation (forced by an explicit constraint, not a fork)

- The orchestration loop wraps **each evaluator's `evaluate()` call independently** in its own try/except. One evaluator raising an exception is caught, recorded with execution status `FAILED` (Task 2), and does **not** stop the remaining evaluators from running and persisting their own results. A whole-request failure on one evaluator's bug would directly violate "evaluator infrastructure failure must not be mislabeled as agent failure" and Phase 4 Task 8's already-locked independent-per-evaluator-commit model.

### Consequence of the already-locked `run_failures` schema, worth stating here

- `run_failures` is 1:1 with `agent_runs` (`PK/FK = run_id`), unlike `evaluation_results`' append/history grain. Each `/evaluate` call, once it runs the classifier (Task 5) and persists `RunFailure` (Task 6), **upserts** that one row — only the most recent evaluation's failure classification is ever visible per run, even though full evaluator-invocation history is preserved in `evaluation_results`.

### Response shape — **decided: dedicated `EvaluationTriggerResponse`**

- A new schema owned entirely by this endpoint: `run_id`, `overall_status` (Task 3), `evaluator_results: list[EvaluatorResultSummary]` (name, version, execution status, passed/score/label/severity, findings), `failure` (Task 5/6's primary/secondary/max_severity, nullable), `evaluated_at`.
- **Rejected alternative**: reusing/extending `RunDetailResponse` for this endpoint's response (mirroring the other project's Phase 6 Task 4 precedent of reusing `AgentQueryResponse` for its approvals endpoint). Rejected because it would force this endpoint to assemble full trajectory data (spans/tool_calls/llm_calls) on every evaluate call that it has no use for, and would make Task 1's response shape depend on Task 7 (several tasks later) actually adding evaluation fields to `RunDetailResponse` — an awkward forward dependency for the first task in the phase. Keeping the two schemas independent lets this endpoint's implementation stay decoupled from Task 2 (Phase 3)'s response shape.

### Test / Validation

- [ ] `POST /v1/runs/{run_id}/evaluate` on an unknown `run_id` returns `404`.
- [ ] Calling the endpoint on a run in any lifecycle state (including `AWAITING_APPROVAL`) runs all five deterministic evaluators without a lifecycle guard rejecting the call.
- [ ] A forced exception in one evaluator's `evaluate()` does not prevent the other four from running, persisting their own results, and appearing correctly in the response's `evaluator_results`.
- [ ] The response validates as `EvaluationTriggerResponse` — confirmed no field from `RunDetailResponse` (spans/tool_calls/llm_calls/HITL/usage) is present.
- [ ] Calling `/evaluate` twice on the same run inserts two full sets of fresh `evaluation_results` rows (per Phase 4 Task 8's append grain) but leaves exactly one `run_failures` row, reflecting only the second call's classification.

---

## Task 2 — Define evaluator execution status independently from pass/fail

### The enum

- `EvaluatorExecutionStatus` (`StrEnum`): `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `SKIPPED` — lives in the evaluation module, **not** `telemetry.v1.enums` (this is Project-2-internal orchestration vocabulary, never part of the ingested wire contract, same distinction already drawn for `RunFailure`'s own categories).
- This is genuinely new: Phase 4 Task 1 deliberately kept `EvaluationResult` free of any execution-status field, precisely so Phase 5 could define this lifecycle independently of "did it pass."

### `PENDING`/`RUNNING`/`SKIPPED` are reserved, not reachable in v1

- `PENDING`/`RUNNING` never appear in a persisted row in Phase 5, since Task 1 already locked synchronous, in-request execution — by the time any code path could persist a status, the evaluator has already returned or thrown. Documented as reserved-but-dormant, same treatment already given to `ExecutionStatus.ERROR` (known-issues.md ISSUE-001) and `LLMCallType.EVIDENCE_GATHERING` (ISSUE-002), so it doesn't read as an oversight later.
- `SKIPPED` is likewise unreachable by any of the five deterministic evaluators — none has a precondition under which the orchestrator would decline to call `evaluate()`. Reserved for Phase 6 ("skip if judge credentials are absent"). Consequence locked now: **skip is an orchestrator decision made before calling `evaluate()`, never something an evaluator signals via its return value** — `evaluate()`'s signature stays exactly `(run) -> EvaluationResult`, no sentinel/optional return added.

### `CHECK` constraint added immediately

- `evaluation_results.status` gets `CHECK (status IN ('pending','running','completed','failed','skipped'))` via a new migration. Phase 2 Task 1 left it unconstrained specifically because the vocabulary wasn't defined yet, while applying `CHECK` to every column whose vocabulary *was* locked — Task 2 is the point the vocabulary locks, so the symmetric move is to constrain it now, same as every other controlled-vocabulary column in this schema.

### Decided: what a `FAILED` row contains — **Option 1, null out the outcome fields**

- On an evaluator exception: `status="failed"`, `passed=NULL`, `score=NULL`, `label=NULL`, `severity=NULL`, `reason=<exception type/message>`, `findings=[{"code": "evaluator_exception", "message": ..., "data": {...}}]`.
- **Rejected alternative**: forcing a placeholder `label` (e.g. `"error"`) so `label` is never null for a failed row. Rejected because it invents a `label` value no evaluator's actual contract produces and duplicates information `status` already carries exactly — the same "two parallel sources of truth for one fact" pattern this project has rejected before (Phase 4 Task 1's own "no execution-status field on `EvaluationResult`," and the other project's Phase 6 Task 1 rejecting a second status field on `WorkOrderDraft`).
- `persist_evaluation_result` (Phase 4 Task 8) is extended to accept an explicit `status: EvaluatorExecutionStatus` parameter (replacing its previous hardcoded `"completed"`) and `result: EvaluationResult | None` (`None` only when `status=FAILED`, using the null-fields shape above) — one function handles both the success and failure persistence paths, not two separate functions.

### Test / Validation

- [ ] `evaluation_results.status` rejects a value outside the five-item vocabulary at the DB layer.
- [ ] No test or code path ever persists `status="pending"` or `status="running"` — confirmed by code inspection (synchronous execution never produces these).
- [ ] A forced evaluator exception persists a row with `status="failed"`, `passed`/`score`/`label`/`severity` all `NULL`, and exactly one `evaluator_exception` finding.
- [ ] `label` has no non-`pass`/`fail`/`not_applicable` value anywhere in the schema — confirmed no placeholder value was introduced for the failure path.
- [ ] `persist_evaluation_result`'s signature takes an explicit `status` argument; no caller relies on a hardcoded default.

---

## Task 3 — Overall PASS / FAIL / INCOMPLETE summary rules

### Computed once, persisted on `run_failures` (fills a real gap, not itself a fork)

- **Computed inside the `/evaluate` orchestration loop**, immediately after all evaluators have run — every evaluator's `(execution_status, label)` pair is already in memory, so this is never recomputed later by re-querying `evaluation_results` (which would additionally require "latest row per evaluator" logic against an append-only table).
- **Persisted as a new `overall_status` column on `run_failures`** — not a new table (the design doc explicitly rules out a separate "evaluation-summary table"; `run_failures` already exists specifically to hold a per-run, evaluation-derived, upserted-on-each-`/evaluate` snapshot). `CHECK (overall_status IN ('pass','fail','incomplete'))` added immediately, same reasoning as Task 2.
- **A `run_failures` row is now upserted on every `/evaluate` call, including a `PASS` verdict**, with `primary_category = NULL` in that case (a natural value: "no failure to classify"). This makes row-absence unambiguously mean "never evaluated," which Task 7 needs to distinguish "not yet evaluated" from "evaluated and passed." This previews part of Task 6's own scope but is necessary for Task 3 to be coherent.
- **Severity plays no role in this computation** — `overall_status` is purely a function of each completed evaluator's `label` plus each evaluator's execution status, never `severity`/`score`. Keeps "no weighted quality score" clean and keeps severity scoped to feeding `RunFailure.max_severity` (Task 4/5) only.

### The rule

- **`FAIL`** if any evaluator with `execution_status == COMPLETED` has `label == "fail"`. No distinction by severity — the design doc's "a critical policy failure **or** required evaluator failure produces FAIL" reads as two independently-sufficient triggers, not a severity gate; every evaluator's fail counts equally.
- **`INCOMPLETE`** — checked only if the above didn't already fire — if any evaluator's `execution_status == FAILED`.
- **`PASS`** otherwise (every evaluator completed, and every completed evaluator's label is `pass` or `not_applicable`).

### Decided: `FAIL` takes precedence over `INCOMPLETE` when both conditions fire in the same run

- E.g. `PolicyEvaluator` completes with a genuine critical violation while `EvidenceEvaluator` crashes in the same call → overall_status = `FAIL`.
- **Rationale**: matches the design doc's asymmetric phrasing exactly — "a critical policy failure... produces FAIL" is unconditional, while "evaluator infrastructure failure **may** produce INCOMPLETE" reads as the fallback for when nothing else already determined the verdict.
- **Rejected alternative**: `INCOMPLETE` taking precedence over `FAIL` — rejected as over-extending the "don't mislabel infra failure as agent failure" constraint into suppressing a real, already-confirmed, already-actionable failure just because something unrelated also broke.

### Test / Validation

- [ ] A run where every evaluator completes with `label="pass"`/`"not_applicable"` produces `overall_status="pass"` and a `run_failures` row with `primary_category=NULL`.
- [ ] A run with one completed evaluator reporting `label="fail"` produces `overall_status="fail"`, regardless of any other evaluator's outcome.
- [ ] A run with one evaluator `FAILED` (infra) and no completed evaluator reporting `label="fail"` produces `overall_status="incomplete"`.
- [ ] A run with both a confirmed `label="fail"` on one evaluator and a `FAILED` execution status on a different evaluator produces `overall_status="fail"` (not `"incomplete"`).
- [ ] `run_failures.overall_status` rejects a value outside `{pass, fail, incomplete}` at the DB layer.
- [ ] A `PASS`-verdict `/evaluate` call still inserts/updates a `run_failures` row (not skipped), with `primary_category=NULL`.

---

## Task 4 — Failure taxonomy and deterministic severity mapping

### The taxonomy is frozen, not re-opened

The seven failure types — `tool_failure`, `retrieval_failure`, `trajectory_error`, `output_validation_error`, `unsupported_claim`, `policy_violation`, `unknown` — are already locked verbatim in the design doc §10.1 and the implementation plan's Locked Technical Baseline table.

### AMENDMENT to Phase 4 Task 5 — severity vocabulary reconciled

- **Decided**: adopt the design doc §10's canonical four-value severity vocabulary — **`info` / `warning` / `error` / `critical`** — as the one true vocabulary everywhere severity appears in this project.
- **Amends Phase 4 Task 5**: that task, written before this vocabulary existed, used an ad hoc `"major"` value for the unknown-asset-downstream-call finding. `"major"` is remapped to **`"error"`** — a confirmed workflow-correctness bug sits one tier below `critical` on the four-value ladder, not at the softer `warning`. Phase 4's `"critical"` value (HITL-bypass) is unchanged, already matching the canonical vocabulary.
- **Rejected alternative**: keeping `critical`/`major` as the real vocabulary and treating the design doc's four-value list as non-binding. Rejected because it would let each phase invent its own severity words instead of converging on one fixed enum — exactly the "arbitrary evaluator-specific semantics" the design doc's centrally-defined-mapping language exists to prevent. Since Phase 4 is still unimplemented, this correction costs nothing beyond a documentation update.

### `FAILURE_TYPE_SEVERITY` — one static, centralized table

| Failure type | Severity | Rationale |
|---|---|---|
| `policy_violation` | `critical` | Explicit "highest-severity class" per design doc. |
| `unsupported_claim` | `error` | Material unsupported claim reaching a human; kept below the deterministic-guardrail `critical` tier since it's LLM-judge-sourced (Phase 6). |
| `output_validation_error` | `error` | Breaks the structured-output contract downstream consumers rely on. |
| `trajectory_error` | `error` | Reconciled value for the former `"major"` — a confirmed workflow-correctness bug. |
| `tool_failure` | `error` | A required tool call terminated in `FAILURE`/`ERROR` despite retries. |
| `retrieval_failure` | `warning` | Evidence gap degrades trustworthiness but isn't itself a contract/safety break. |
| `unknown` | `None` | No reliable category was assignable; forcing a severity would fabricate confidence the classification doesn't have. `RunFailure.max_severity` is nullable, so this is a real, expected value. |

### Centralization rule

- `FailureClassifier` (Task 5) derives `RunFailure.max_severity` **from this table, keyed by failure type — never by reading an evaluator's own `severity` field directly.** `PolicyEvaluator`'s own already-locked per-result `severity` (Phase 4 Task 5) stays unchanged on that evaluator's own `EvaluationResult` row (informational), but the run-level classifier doesn't consult it. Exactly one source of truth feeds the number that matters for analytics/prioritization, regardless of which evaluator surfaced the underlying finding.

### Test / Validation

- [ ] No code path or persisted value uses `"major"` anywhere — confirmed by code inspection; Phase 4 Task 5's unknown-asset finding maps to severity `"error"`.
- [ ] `FAILURE_TYPE_SEVERITY` is a single static dict; every one of the seven failure types has an entry (six real values, one explicit `None` for `unknown`).
- [ ] `RunFailure.max_severity` is computed by looking up detected failure types in `FAILURE_TYPE_SEVERITY`, never by reading `EvaluationResult.severity` off any individual evaluator result.
- [ ] `PolicyEvaluator`'s own `EvaluationResult.severity` field (Phase 4 Task 5) is unchanged and still populated exactly as that phase locked it.

---

## Task 5 — `FailureClassifier`

### Finding → failure-type mapping

Static `FINDING_TO_FAILURE_TYPE: dict[str, FailureType]`:

| Finding code | Source evaluator | Failure type |
|---|---|---|
| `tool_call_failed` / `tool_call_error` | ToolExecutionEvaluator | `tool_failure` |
| `empty_output` | StructuredOutputEvaluator | `output_validation_error` |
| `missing_required_tool` / `forbidden_tool_used` / `ordering_violation` / `terminal_condition_mismatch` | TrajectoryEvaluator | `trajectory_error` |
| `missing_required_evidence` | EvidenceEvaluator | `retrieval_failure` |
| `unauthorized_consequential_action` | PolicyEvaluator | `policy_violation` |
| `unknown_asset_downstream_call` | PolicyEvaluator | `trajectory_error` |

- **`PolicyEvaluator`'s two findings map to two different failure types**, not both to `policy_violation`. This is intentional: the source evaluator and the assigned taxonomy category are independent axes ("evaluator dimensions and failure categories remain distinct" — design doc). An unknown-asset downstream call is, by the taxonomy's own definition, an invalid workflow (`trajectory_error`), even though `PolicyEvaluator` is the evaluator that caught it.

### Primary/secondary selection — one static priority list, independent of severity

- `policy_violation` is **always** primary when present — an explicit design constraint ("policy violations take... appropriate precedence in primary-failure selection"), not a judgment call.
- Otherwise, a static `FAILURE_TYPE_PRIORITY` list breaks ties among detected types: `[policy_violation, output_validation_error, tool_failure, trajectory_error, retrieval_failure, unsupported_claim, unknown]` (roughly upstream/execution problems before downstream/semantic ones). Primary = earliest-detected type in this list; the rest, in the same order, become secondary (subject to Fork B's singular-column constraint below).
- **Rejected alternative**: sorting candidates by `FAILURE_TYPE_SEVERITY` instead of a fixed priority list. Rejected because several types share `error` severity, so a severity-based sort still needs its own tie-break list anyway — it doesn't eliminate the need for a static ordering, just adds a second, overlapping one.
- **`RunFailure.max_severity` is computed independently** as `max(FAILURE_TYPE_SEVERITY[t] for t in all_detected_types)` — across primary *and* secondary together, never just "whatever the primary's severity happens to be." This is what "maximum severity" means literally in the design doc, and it fully decouples primary-selection ordering from severity computation.

### Fork A — decided: `primary_category = NULL` when `INCOMPLETE` with no actual finding

- When `overall_status == INCOMPLETE` purely because an evaluator crashed, with no completed evaluator reporting `label="fail"`, `primary_category` (and `secondary_category`, `max_severity`) stay `NULL`.
- **Rejected alternative**: `primary_category = "unknown"` in this case. Rejected as a direct violation of this phase's own explicit constraint — labeling an evaluator infra crash as an `"unknown"` *agent* failure is exactly the mislabeling the constraint prohibits. `overall_status="incomplete"` (Task 3) already fully communicates "don't trust this as exhaustive" without the classifier needing to fabricate a category.
- **Consequence** (falls out of Task 3's `FAIL`-over-`INCOMPLETE` precedence + this decision): `INCOMPLETE` runs can **never** have a non-null `primary_category` — by construction, only `FAIL` runs ever populate `by_failure_type`-style breakdowns (relevant to Task 8).

### Fork B — decided: `secondary_category` stays singular, no migration

- Only the single next-highest-priority detected type (if any) is persisted as `secondary_category`; anything beyond primary+one-secondary in a 3+-simultaneous-type run isn't rolled up in `run_failures` (still fully visible via `evaluation_results` for anyone who digs in).
- **Rejected alternative**: widening `secondary_category` → `secondary_categories TEXT[]` (mirroring `final_result_source_references`). Rejected per this project's consistent "don't build ahead of need" posture — per `known-issues.md` ISSUE-004, even the 2-simultaneous-violation case isn't yet confirmed exercised by any fixture, making a 3+-type array a purely speculative addition right now. This does mean the implementation plan's plural "secondary type**s**" phrasing isn't literally satisfied by the schema — noted here rather than silently glossed over.

### Test / Validation

- [ ] A run with only `unauthorized_consequential_action` fired produces `primary_category="policy_violation"`, `secondary_category=NULL`.
- [ ] A run with only `unknown_asset_downstream_call` fired produces `primary_category="trajectory_error"` (not `"policy_violation"`).
- [ ] A run with both a policy finding and any other evaluator's fail finding produces `primary_category="policy_violation"` — confirmed policy always wins regardless of `FAILURE_TYPE_PRIORITY` order among the rest.
- [ ] A run with two non-policy fail findings of different types produces primary = whichever is earlier in `FAILURE_TYPE_PRIORITY`, secondary = the other.
- [ ] `max_severity` reflects the maximum severity across **all** detected types (primary + secondary), not just the primary's own severity — confirmed by a fixture where the secondary type has higher severity than the primary.
- [ ] An `INCOMPLETE` run with zero completed-evaluator fail findings produces `primary_category=NULL`, `secondary_category=NULL`, `max_severity=NULL`.
- [ ] `run_failures.secondary_category` remains a single nullable `TEXT` column — confirmed no array/multi-value column was introduced.

---

## Task 6 — Persist `RunFailure` separately from `EvaluationResult`

### Mechanics

- **Function shape**: `persist_run_failure(session: AsyncSession, run_id: str, classification: RunFailureResult) -> RunFailureRecord` — same plain-function-with-injected-session pattern as Phase 4 Task 8's `persist_evaluation_result`.
- **Full-column-overwrite upsert**, no `COALESCE`: `INSERT ... ON CONFLICT (run_id) DO UPDATE SET <every column> = excluded.<every column>` — mirrors `agent_runs`' own upsert precedent (Phase 2 Task 6) exactly.
- **Classification runs against the current `/evaluate` call's freshly-computed, in-memory evaluator results — never a DB re-query.** Safe (not just convenient) because Task 1 already locked "every `/evaluate` call always runs the entire registry, no partial runs" — there's never a scenario where some evaluators ran now and others are stale leftovers needing reconciliation. (Contrast with the other project's Phase 6 Task 1, where a superficially similar question genuinely needed a re-query, because partial evidence-gathering was possible there — that gap doesn't exist here.)
- **`persist_run_failure` is its own independent commit**, executed only after every evaluator's individual persist attempt has already happened — consistent with Phase 4 Task 8's "evaluator infra failure must not roll back other evaluators' committed results." If this final write fails, the already-committed `evaluation_results` rows remain valid; the run has fresh evaluation history but a stale (or absent) `run_failures` snapshot until the next successful `/evaluate` call.
- **Called unconditionally on every `/evaluate` call, including a `PASS` verdict** — per Task 3.

### AMENDMENT to Phase 2 Task 1 — `classifier_version` column added

- **Decided**: add `run_failures.classifier_version` now, bundled into the same migration Task 3 already needs (for `overall_status`), so `run_failures` is only touched once across this phase.
- **Rationale**: the design doc's own persistence-model table explicitly names "classifier version" as a key persisted concern for `run_failures`, but Phase 2's concrete locked schema omitted it — an apparent oversight (unlike the explicitly-reasoned `regression_runs` deferral), in the same category as `known-issues.md` ISSUE-005 (`evaluation_results.passed` dropped at persistence). Closing it now, while cheap (one column, no existing data to migrate around), avoids letting it calcify into an undocumented gap.
- **Rejected alternative**: leaving it out, treating it as acceptable schema drift for now, matching this project's usual "don't build for a consumer that doesn't exist yet" posture. Rejected because this isn't a hypothetical future need being pre-built — it's a concrete, already-named design-doc line item, and the marginal cost of adding it alongside an already-planned migration is negligible.
- `FailureClassifier` gets `name`/`version` class attributes analogous to Phase 4's evaluator convention; `persist_run_failure` writes the classifier's current version into this column on every upsert.

### Test / Validation

- [ ] `persist_run_failure` performs an upsert (`ON CONFLICT (run_id) DO UPDATE`), not an insert-only pattern — confirmed calling it twice for the same `run_id` leaves exactly one row, reflecting the second call's values.
- [ ] `FailureClassifier`'s input is exactly the in-memory `EvaluationResult` set produced by the current `/evaluate` call — confirmed by a unit test constructible without any DB read.
- [ ] A forced failure in `persist_run_failure` (simulated DB error) does not roll back or otherwise affect the `evaluation_results` rows already committed earlier in the same `/evaluate` call.
- [ ] `run_failures.classifier_version` is populated on every persisted row, matching `FailureClassifier.version`.
- [ ] The migration adding `overall_status` (Task 3) and `classifier_version` (this task) to `run_failures` is a single migration, not two.

---

## Task 7 — Failure/evaluation information on run list and run-detail responses

### `RunSummary` (list item) — flat fields

- Three new optional scalar fields via `LEFT JOIN run_failures`: **`overall_status`, `primary_failure_type`, `max_severity`** — all `None` together when the run has never been evaluated. Matches `RunSummary`'s existing all-flat-scalar shape (Phase 3 Task 1); purely additive per Phase 3 Task 6's locked rule for this exact extension.

### New filters on `GET /v1/runs`

- `?overall_status=` and `?primary_failure_type=` added as exact-match filters, AND-combined with existing ones — the exact extension point Phase 3 Task 1 explicitly reserved ("land later as new optional params, not a redesign"). Costs nothing extra since `run_failures` is already being joined in for the `RunSummary` fields above.

### `RunDetailResponse` — two new nested fields, names anticipated by Phase 3

- **`failure: RunFailureSummary | None`** — sourced from the single current `run_failures` row: `overall_status`, `primary_failure_type`, `secondary_failure_type`, `max_severity`, `classifier_version`, `updated_at`. `None` when never evaluated.
- **`evaluation_summary: list[EvaluatorResultSummary] | None`** — one entry per evaluator (`evaluator_name`, `evaluator_version`, `execution_status`, `passed`, `score`, `label`, `severity`, `reason`, `findings`). `None` when never evaluated.
- Both names were already anticipated in Phase 3 Task 6's additive-only rule ("known upcoming extensions: `evaluation_summary`, `failure` fields") — adopted directly rather than inventing new ones.

### Decided: `evaluation_summary` shows latest-per-evaluator only (Option 1)

- One entry per `evaluator_name`, most recent by `created_at` — mirroring what `run_failures` already represents ("the current state of this run's evaluation"). Requires a latest-per-group query (window function or `MAX(created_at)` subquery) rather than a plain scan.
- **Rejected alternative**: exposing the full `evaluation_results` history (every row, unfiltered, ordered by `created_at`) — mirroring Phase 3 Task 2's flat-list philosophy for spans/tool_calls/llm_calls. Rejected because it would push "which result is current" onto every client, and would show a stale `fail` sitting next to a fresh `pass` from a later re-run, contradicting the always-current `failure` block sitting right next to it in the same response. The field's own pre-existing name — `evaluation_**summary**`, not `evaluation_history` — is itself evidence the intended shape was always "current state," not history.
- **Consequence**: re-evaluation history becomes invisible via the API entirely in v1 (still inspectable directly in the DB; no history endpoint exists in this phase's task list).

### Test / Validation

- [ ] `GET /v1/runs` items include `overall_status`/`primary_failure_type`/`max_severity`, all `None` for a never-evaluated run.
- [ ] `?overall_status=fail` and `?primary_failure_type=policy_violation` each independently narrow `GET /v1/runs` results correctly against a seeded fixture set; combined with existing filters via AND.
- [ ] `GET /v1/runs/{run_id}` for a never-evaluated run has `failure=None` and `evaluation_summary=None`.
- [ ] For a run evaluated twice, `evaluation_summary` shows exactly one entry per evaluator (the latest), not two — confirmed against a fixture re-evaluated with a deliberately changed outcome.
- [ ] `failure` and `evaluation_summary` are both absent from `RunSummary` (list item) — confirmed by response-shape inspection; those nested blocks are detail-only.

---

## Task 8 — `GET /v1/analytics/failures`

### Scope boundary

- Same optional `started_after`/`started_before` filter as every other analytics endpoint; no `scenario_id`/`agent_version`/`model` filters — same "one fixed, named question" discipline as Phase 3 Tasks 3–5.
- **No pass rate on this endpoint** — that's Task 9's job on `/v1/analytics/overview`. This endpoint answers "of the runs we've evaluated, what kinds of problems showed up and how bad," not overall prevalence-vs-all-traffic.
- **Denominator excludes never-evaluated runs entirely** — mirrors Phase 3 Task 3's runtime-success-rate precedent (excluding `AWAITING_APPROVAL`) directly.

### Shape

- **`run_counts`**: total evaluated, broken down by `overall_status` (`pass`/`fail`/`incomplete`) — mirrors Task 3 (Phase 3)'s `run_counts`-by-`status` breakdown; INCOMPLETE counts are actionable evaluator-health information distinct from FAIL counts and shouldn't be buried inside the failure-type breakdown.
- **`by_failure_type`**: one entry per `primary_failure_type` actually present (dynamic, not a hardcoded seven-value enumeration — same "only show what's present" pattern as Phase 3 Tasks 4/5's tool/model breakdowns).
- **`by_severity`**: same shape, grouped by `max_severity` — a second breakdown dimension, mirroring Phase 3 Task 5's "both a `by_model` and a `by_call_type` breakdown" decision rather than picking one axis.
- **Primary-type-only** (not counting secondary/"any occurrence"): counting a type whether primary or secondary would let one run contribute to two buckets, breaking the "each breakdown sums back to the overall total" invariant tested at every analytics endpoint so far. Given Task 5 Fork B limited `secondary_category` to a single value, and per ISSUE-004 even 2-simultaneous-type failures aren't yet fixture-confirmed, a separate "any occurrence" view is speculative today.
- **Consequence, not new work**: because Task 5 Fork A makes `INCOMPLETE` runs always have `primary_category=NULL`, `by_failure_type` is, by construction, sourced entirely from `FAIL` runs.

### Decided: report both percentage framings (Option 2)

- Every `by_failure_type`/`by_severity` bucket carries **both** `pct_of_evaluated` (denominator = all evaluated runs) and `pct_of_failing` (denominator = `FAIL` + `INCOMPLETE` runs only).
- **Rejected narrower alternative**: `pct_of_failing` only. Rejected in favor of both, mirroring Phase 3 Task 5's own precedent of rejecting a narrower single-breakdown alternative in favor of computing both cheaply-available, complementary dimensions (there: `by_model` + `by_call_type`; here: prevalence-vs-all-traffic + share-of-failures).

### Test / Validation

- [ ] `run_counts` sums to the total evaluated-run count for the scoped time range, broken down correctly by `overall_status`.
- [ ] `by_failure_type` contains no entry for a failure type absent from the scoped data; entries' counts sum to the total `FAIL`-status run count (never including `INCOMPLETE`).
- [ ] Each `by_failure_type`/`by_severity` bucket exposes both `pct_of_evaluated` and `pct_of_failing`, computed against the correct respective denominators.
- [ ] A never-evaluated run does not appear in any count or percentage on this endpoint.
- [ ] `by_severity`'s `unknown`-severity-adjacent case (a run whose primary type maps to `severity=None`) is either omitted or explicitly bucketed as "unassigned" — confirmed by code inspection which path was taken.

---

## Task 9 — Extend `GET /v1/analytics/overview` with behavioral evaluation pass rate

### Shape

- New field **`behavioral_pass_rate: float | None`** (`None` only when zero evaluated runs are in scope) sitting next to the existing `runtime_success_rate` — two independent rates over two independent concepts, never one derived from the other.
- New **`evaluation_counts`** breakdown (evaluated total, `pass`/`fail`/`incomplete` counts), mirroring how `run_counts` already backs `runtime_success_rate`. This overlaps somewhat with Task 8's own `run_counts`, which is acceptable and precedented — Phase 3 already has each analytics endpoint independently report whatever backs its own headline metric.
- Same optional `started_after`/`started_before` filter already on this endpoint scopes both the runtime and behavioral metrics together in one request.
- **Denominator excludes never-evaluated runs** — same reasoning as Task 8.

### Decided: `INCOMPLETE` excluded from the pass-rate denominator (Option 2)

- `behavioral_pass_rate = PASS / (PASS + FAIL)`, with `INCOMPLETE` runs visible via `evaluation_counts` but not touching the rate itself.
- **Rationale**: exact structural mirror of Phase 3 Task 3's already-locked precedent (excluding `AWAITING_APPROVAL` — a "not yet determined" runtime state — from `runtime_success_rate`'s denominator); satisfies this phase's own explicit "evaluator infrastructure failure must not be mislabeled as agent failure" constraint literally, not just in spirit.
- **Rejected alternative**: `behavioral_pass_rate = PASS / (PASS + FAIL + INCOMPLETE)`. Rejected because it would let an evaluator infra crash — unrelated to the agent — directly drag down the one number meant to describe the agent's own behavioral quality.

### Noted, not a decision: golden and live runs are mixed in this one number

- Because `TrajectoryEvaluator`/`EvidenceEvaluator` report `not_applicable` (counted as passing) on non-golden/live runs, a live run's `PASS` verdict really only reflects `ToolExecutionEvaluator`/`StructuredOutputEvaluator`/`PolicyEvaluator`. `behavioral_pass_rate` doesn't distinguish golden from live runs. Inherited entirely from Phase 4's already-locked evaluator design; not something this task changes.

### Test / Validation

- [ ] `behavioral_pass_rate` excludes `INCOMPLETE` runs from its denominator — confirmed by a fixture where an `INCOMPLETE` run is present and the rate matches `PASS / (PASS + FAIL)` exactly, not `PASS / (PASS + FAIL + INCOMPLETE)`.
- [ ] `evaluation_counts` correctly reports `pass`/`fail`/`incomplete` counts, summing to the total evaluated-run count in scope.
- [ ] `runtime_success_rate` and `behavioral_pass_rate` are computed independently and can disagree in a fixture where a run runtime-succeeds but behaviorally fails (or vice versa is structurally impossible, confirmed by inspection).
- [ ] `started_after`/`started_before` scope both rates and both count breakdowns consistently in a single request.
- [ ] A run in `AWAITING_APPROVAL` is excluded from `runtime_success_rate`'s denominator exactly as Phase 3 Task 3 locked, and independently excluded from `behavioral_pass_rate`'s denominator only if it has never been evaluated (not because of its HITL state per se).

---

## Success Criteria

- [ ] A run can be evaluated on demand via `POST /v1/runs/{run_id}/evaluate`, running the full deterministic evaluator registry synchronously, with one evaluator's infrastructure failure never blocking or corrupting the others' results (Task 1, Task 2).
- [ ] Every evaluator invocation's outcome is stored with an execution status independent of pass/fail, closing the lifecycle gap Phase 4 deliberately left open for this phase (Task 2).
- [ ] A run's overall behavioral verdict is categorical (PASS/FAIL/INCOMPLETE, never a weighted score), computed once per evaluation and persisted for cheap reads — with a confirmed failure never hidden behind an unrelated evaluator crash (Task 3).
- [ ] The failure taxonomy and severity vocabulary are now fully reconciled between the design doc and the concrete schema — the `"major"`/`"critical"` drift discovered mid-planning is closed before any code exists (Task 4).
- [ ] `FailureClassifier` deterministically produces one primary failure type, an optional secondary type, and a maximum severity computed centrally from the taxonomy table — never conflating "an evaluator crashed" with "the agent misbehaved" (Task 5).
- [ ] `RunFailure` persists as an independently-committed, always-current snapshot per run, versioned by classifier version, closing a second design-doc/schema drift found mid-planning (Task 6).
- [ ] Run list and run detail responses now surface current evaluation/failure state additively, without disturbing any existing Phase 3 field or shape (Task 7).
- [ ] `GET /v1/analytics/failures` and the extended `GET /v1/analytics/overview` give both a "what's failing and how" view and a "how often is the agent behaviorally correct" view, with runtime and behavioral success kept structurally and numerically distinct throughout (Task 8, Task 9).
- [ ] The full pipeline — mock telemetry → ingestion → Postgres → deterministic evaluation → failure classification → Query API — is demonstrable end-to-end using only the canonical fixture corpus, achieving Tier A / Core Platform Complete.

## Status

All nine Phase 5 tasks are locked. Phase 5 planning is complete, including two explicit amendments to earlier "locked" decisions (Phase 4 Task 5's severity vocabulary; Phase 2 Task 1's `run_failures` schema) reconciled while both are still unimplemented. Next: proceed to implementation of Phases 0–5 together (none of Phases 4–5 have been coded yet), after which the project reaches the **Core Platform Complete** mandatory checkpoint called out in the implementation plan — a stop-and-verify point before any Phase 6 (LLM Evaluation) work begins.