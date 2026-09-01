# Phase 6 (LLM Evaluation) — Implementation Decisions

Captured from planning discussion, 2026-09-01. These are decisions made ahead of implementation, refining Implementation Plan v1.0 / Phase 6 and building on the Phase 0 (Walking Skeleton), Phase 1 (Telemetry Contract & Debug Fixtures), Phase 2 (Ingestion & Persistence), Phase 3 (Core Query API & Operational Analytics), Phase 4 (Deterministic Evaluation), and Phase 5 (Failure Classification & Evaluation API) decisions without contradicting any of them. Decisions are locked task-by-task, following the Phase 6 task list from the implementation plan:

1. Create a thin provider-agnostic judge client abstraction and configure one initial provider/model. **(locked)**
2. Implement `GroundednessJudge` that returns structured pass/score/reason plus `unsupported_claims` findings. **(locked)**
3. Persist judge model, latency, token usage, and estimated evaluation cost separately from agent execution cost. **(locked)**
4. Create a small human-auditable groundedness calibration fixture set with clearly grounded, clearly unsupported, and a few ambiguous examples. **(locked)**
5. After `GroundednessJudge` is stable, implement `UncertaintyJudge` for hypothesis-versus-fact calibration. **(locked)**
6. Use Pydantic/JSON structured judge outputs and bounded retry for invalid model responses. **(locked)**
7. Define skip/availability behavior when judge credentials/provider are absent. **(locked)**

All seven Phase 6 tasks are locked. None are implemented yet — this document is planning-only, ahead of writing any code, consistent with how Phases 0–5 were planned. See Success Criteria and Status at the bottom.

**Naming note**: this project's root already contains a file literally named `phase6-decisions.md` — that file belongs to a *different* project (Project 1, the Industrial Maintenance Agent's own "HITL & Work-Order Workflow" phase) that happens to share this project space. This document is this project's (Project 2, the observability platform's) Phase 6, filed under `claude/` to match the `claude/phase0-5-decisions.md` naming already established here.

---

## Task 1 — Provider-agnostic judge client abstraction + initial provider/model

### Client return shape — rich `JudgeCallResult`, not a bare structured object

- `async def generate_structured(prompt: str, response_model: type[T]) -> JudgeCallResult[T]`, where `JudgeCallResult` wraps `output: T` plus `model`, `provider`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd` — the same field set `LLMCall` already uses for the agent's own calls (Phase 1 Task 1).
- Rejected alternative: a thin return of just the bare parsed model, leaving each judge to independently time itself and extract provider-specific usage data. Rejected because Task 3 requires persisting exactly this metadata for every judge call — centralizing extraction in the client means every judge gets consistent, non-duplicated instrumentation for free, with no risk of two judges computing latency or cost slightly differently.

### Abstraction mechanism — ABC with a template method, not a `Protocol`

- `JudgeClient(ABC)` implements `generate_structured()` itself — owning latency timing, `JudgeCallResult` construction, and (per Task 6) the bounded-retry loop — and declares an abstract `_raw_complete(prompt, schema)` that each concrete provider adapter implements. `AnthropicJudgeClient` is the sole concrete adapter for now.
- Rejected alternative: a structural `typing.Protocol` with no shared base implementation. Rejected because it would leave retry/instrumentation logic duplicated per provider once a second provider adapter is ever added — the ABC keeps that logic in exactly one place, which matters given "provider-agnostic" is this task's own stated goal.

### Provider, model, credentials

- **Provider**: Anthropic, via the official SDK — consistency with the rest of the stack (Project 1's real agent, the mocked `LLMCall` fixtures) rather than a second SDK dependency for one evaluator.
- **Structured output mechanism**: forced tool-calling (a JSON-schema-shaped tool, forced tool choice), not free-text JSON parsing — matches Project 1's own `generate_structured()` idiom, so both projects converge on the same approach.
- **Model**: a real, currently-available Claude model id, pinned as a config default — resolved to a specific model string at implementation time rather than locked in this planning document (the fixture corpus's `claude-sonnet-4-6` is an illustrative mock string, not a real model id).
- **Credentials/config**: a new `JudgeSettings` sub-model (e.g. `JUDGE__ANTHROPIC_API_KEY`, `JUDGE__MODEL`), following the same nested-settings/env-var-prefix/`.env.example` convention Phase 0 Task 1 established, anticipating exactly this moment.

### Explicitly deferred to later tasks in this phase

- Retry-on-invalid-output behavior — Task 6 fills in the base class's retry loop.
- Skip/unavailable-credentials semantics — Task 7.
- How an `Evaluator` subclass wraps this async client into Phase 5's (currently synchronous) orchestration loop — resolved in Task 2.

### Test / Validation

- [ ] `JudgeClient` is an ABC; `generate_structured()` is implemented once on the base class; each concrete adapter (`AnthropicJudgeClient`) implements only `_raw_complete()` — confirmed by code inspection, no duplicated retry/instrumentation logic across adapters.
- [ ] `generate_structured()` returns a `JudgeCallResult` carrying `output` plus `model`/`provider`/`latency_ms`/`prompt_tokens`/`completion_tokens`/`estimated_cost_usd` — confirmed by a test asserting all fields are populated from a mocked provider response.
- [ ] `JudgeSettings` is a nested sub-model of the top-level `Settings` object, following the same env-var-prefix convention as `DatabaseSettings`/`APISettings`.
- [ ] `AnthropicJudgeClient` uses forced tool-calling (not free-text JSON parsing) to obtain structured output — confirmed by code inspection of the request construction.
- [ ] No evaluator or orchestrator code references `AnthropicJudgeClient` directly outside the settings-driven construction point — confirmed callers depend only on the `JudgeClient` interface.

---

## Task 2 — `GroundednessJudge`

### Async/orchestrator integration — dual interface, branching on `EvaluatorType`

- Phase 4's five deterministic evaluators keep their synchronous `evaluate()` exactly as locked, completely untouched. `GroundednessJudge` (and `UncertaintyJudge`, Task 5) implement a new `async def evaluate_async(self, run, call_log) -> EvaluationResult` on `LLM_BASED`-typed evaluators. Phase 5's orchestrator loop branches on `evaluator.type`: deterministic evaluators are called directly, LLM-based ones are awaited — using the `type` field for exactly the purpose Phase 4 Task 1 said it was reserved for ("so a future orchestrator can filter by kind; nothing reads it yet").
- Rejected alternative: unifying every evaluator (including the five already-implemented deterministic ones) onto a single `async def evaluate()`. Rejected to avoid touching five already-locked, already-tested Phase 4 files for a change with zero behavioral effect on them — pure churn on working code, in favor of a branch that makes an already-existing, already-purposed field do real work.
- Per-evaluator try/except isolation (Phase 5 Task 1) wraps both the sync and async call paths identically, so a judge provider outage still isolates to that one evaluator exactly as a deterministic evaluator's bug would.

### Evidence context — tool-call results only

- The judge receives every `tool_calls.result` blob for the run (raw JSON, read as unstructured text) plus `final_result.output` as the claim under test. `llm_calls` content (the agent's own intermediate reasoning) is deliberately excluded — groundedness checks the answer against observed data, not against the agent's own prior interpretation.
- Evidence is read as opaque JSON text, not resolved via citation IDs. Phase 4 Task 3 already established there's no producer-agnostic way to resolve a `source_reference` ID into the specific record it points to — citation-integrity checking was deferred there specifically to "`EvidenceEvaluator`/Phase 6's groundedness judge." An LLM judge reading raw JSON as text is what finally closes that gap; a citation-ID-scoped design would just re-hit the same wall.

### Judge output schema and mapping to `EvaluationResult`

- Class metadata: `name="groundedness"`, `version="1.0.0"`, `type=EvaluatorType.LLM_BASED`.
- Not-applicable rule mirrors `StructuredOutputEvaluator` exactly: `final_result` legitimately `None` (`TOOL_ERROR`/`RUNTIME_ERROR`/`AWAITING_APPROVAL`) → `passed=True`, `label="not_applicable"`, the judge is never called (saves a real API call, not just a skip label).
- Judge output schema: `passed: bool`, `score: float` (0–1), `reason: str`, `unsupported_claims: list[UnsupportedClaim]` where `UnsupportedClaim = {claim_text: str, explanation: str}`. `model_config = {"extra": "forbid"}` (Task 6).
- `passed` comes directly from the LLM's own categorical judgment, never derived by thresholding `score` in code — consistent with Phase 5 Task 3's precedent that a semantic judgment shouldn't get a mechanically-invented backstop, and this phase's own "do not require exact numeric judge scores in tests" constraint.
- Maps to `EvaluationResult`: `label` = `"pass"`/`"fail"` from `passed`; `severity=None` always (Phase 5 Task 4 derives severity centrally from finding code, never from an evaluator's own field); `findings` = one `EvaluationFinding(code="unsupported_claim", message=claim_text, data={"explanation": ...})` per item in `unsupported_claims`.

### Amendment to Phase 5 Task 5's `FINDING_TO_FAILURE_TYPE`

- Adds `"unsupported_claim" → unsupported_claim` — a new entry, not a change to any existing one. Phase 5 Task 4's severity table already reserved `unsupported_claim → error` for this exact moment ("kept below the deterministic-guardrail critical tier since it's LLM-judge-sourced (Phase 6)").

### Test / Validation

- [ ] `GroundednessJudge` exposes `name="groundedness"`/`version="1.0.0"`/`type=LLM_BASED` as class attributes.
- [ ] The orchestrator branches on `evaluator.type`: `DETERMINISTIC` evaluators are called synchronously via `evaluate()`, `LLM_BASED` evaluators are awaited via `evaluate_async()` — confirmed by a test exercising both kinds in one `/evaluate` call.
- [ ] A run with `final_result=None` produces `passed=True`/`label="not_applicable"` without ever constructing a `JudgeClient` call — confirmed no mocked provider call occurs.
- [ ] The prompt/context assembled for the judge includes `tool_calls.result` content but excludes all `llm_calls` content — confirmed by inspecting the constructed prompt in a test.
- [ ] `passed` is read directly from the judge's own structured output field, never derived by thresholding `score` — confirmed by code inspection.
- [ ] `severity` is always `None` on `GroundednessJudge`'s `EvaluationResult`, regardless of outcome.
- [ ] Each entry in `unsupported_claims` produces exactly one `EvaluationFinding` with `code="unsupported_claim"`.
- [ ] `FINDING_TO_FAILURE_TYPE` contains `"unsupported_claim" → unsupported_claim`; a fixture with a non-empty `unsupported_claims` list results in `primary_category="unsupported_claim"`, `severity="error"` when it's the only fail.

---

## Task 3 — Persist judge model, latency, token usage, and estimated evaluation cost

### Granularity/mechanism — per-attempt logging via an injected call-log

- The orchestrator creates an empty `call_log: list[JudgeCallResult]` and passes it into `evaluate_async(run, call_log)`; `JudgeClient` appends one entry after every raw provider round-trip — successful or schema-invalid-and-retried alike. Whether `evaluate_async()` returns normally or raises, the orchestrator persists one `judge_calls` row per entry already sitting in `call_log`, as its own step, unconditioned on the evaluation's own outcome.
- Rejected alternative: persisting only the final successful call's metadata (via a simple `(EvaluationResult, JudgeCallResult)` return tuple), with nothing persisted on total failure. Rejected because it would under-count real spend on every run where retries occurred, worst on exactly the runs (flaky/expensive judge behavior, or a total failure) where cost visibility matters most.

### Schema

- New table, **`judge_calls`** — never written into `llm_calls`, which stays exclusively populated by the ingestion pipeline from producer-emitted agent telemetry. Conflating the two would directly violate "agent cost and evaluation cost remain distinct" (already the reasoning Phase 5 Task 9 / Phase 3 Task 5 used to scope `/v1/analytics/usage` to `llm_calls` only, explicitly flagging that judge cost "lands in Phase 6 on its own separate accounting").
- Columns: `id`, `run_id` (FK → `agent_runs`), `evaluator_name`, `evaluator_version`, `model`, `provider`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `estimated_cost_usd`, `succeeded: bool`, `created_at`.
- No FK to `evaluation_results` — early attempts in a retry sequence happen before any `EvaluationResult` exists to link to; correlated via `(run_id, evaluator_name)` + timestamp ordering instead, the same non-FK correlation pattern already used between `llm_calls` and a run's `final_result`.
- Insert-only, append grain — mirrors `evaluation_results`, not `run_failures`'s upsert.

### Persistence function and commit scoping

- `persist_judge_call(session: AsyncSession, run_id: str, evaluator_name: str, evaluator_version: str, call: JudgeCallResult, succeeded: bool) -> JudgeCallRecord` — same injected-session, plain-function pattern as `persist_evaluation_result`/`persist_run_failure`.
- Independent commit, same philosophy as Phase 4 Task 8 / Phase 5 Task 6: a DB failure persisting `judge_calls` rows doesn't roll back `evaluation_results`/`run_failures`, and vice versa.
- **No new analytics endpoint in this task.** Exposing a query surface for judge cost isn't in Phase 6's own task list — deferred to whichever later phase (most likely dashboard-adjacent) actually needs it, consistent with this project's "don't build ahead of need" posture.

### Test / Validation

- [ ] Every raw provider round-trip made during an `evaluate_async()` call — successful or retry-triggering-invalid — appends one entry to `call_log`, confirmed with a mocked provider forced to return two invalid responses before a valid third.
- [ ] The orchestrator persists one `judge_calls` row per `call_log` entry regardless of whether `evaluate_async()` ultimately returned an `EvaluationResult` or raised — confirmed by a test where all retries are exhausted (`status="failed"`) yet `judge_calls` contains rows for every attempt.
- [ ] No row ever appears in both `judge_calls` and `llm_calls` for the same call — confirmed the two tables are populated by entirely disjoint code paths.
- [ ] `persist_judge_call` is insert-only; two calls for the same run/evaluator produce two distinct rows.
- [ ] A forced DB failure while persisting `judge_calls` rows does not roll back already-committed `evaluation_results`/`run_failures` rows for the same `/evaluate` call, and vice versa.

---

## Task 4 — Groundedness calibration fixture set

### Fixture shape — minimal judge-scoped pairs, not full synthetic run fixtures

- A small dataset of `{case_id, evidence_text(s), answer_text, expected_label, notes}`, calling `GroundednessJudge` directly on a hand-built minimal input — bypassing full ingestion/`EvaluationRunView` assembly.
- Rejected alternative: full `ExtendedRunEvent`-shaped synthetic run fixtures run through the entire real pipeline. Rejected as heavy ceremony for what's meant to be a small, fast, human-legible set — pipeline-plumbing correctness has its own coverage (Task 6's mocked-provider pytest tests), so this set can stay narrowly about semantic calibration quality.

### Exercising mechanism — notebook-only, no pytest assertions

- The full set (clear and ambiguous cases alike) lives in a walkthrough notebook (matching the project's established Phase 3/4/5 walkthrough-notebook precedent), run locally with real credentials, eyeballed by a human. No case gets a hard pass/fail assertion anywhere in the codebase.
- Rejected alternative: a hybrid where a `pytest.mark.skipif`-gated test asserts on the "clear" cases when credentials happen to be present. Rejected because a conditionally-skipped test in the main suite reads as an odd citizen — a green CI run wouldn't actually prove groundedness still works, since it silently skipped there by default, and it would duplicate "clear case" logic that Task 6's mocked mechanics tests already cover from a different angle.
- Accepted tradeoff: zero CI regression protection for calibration quality specifically — only Task 6's mechanics tests run in CI.

### Content

- ~9 cases: 3 clearly grounded, 3 clearly unsupported, 3 ambiguous.
- Storage: `src/obs_platform/evaluation/judges/calibration/groundedness_cases.json`, loaded via the same explicit-manifest/fail-fast pattern already used for Phase 1's telemetry fixtures and Phase 4's `ScenarioContract`s.
- One "clearly unsupported" case reuses the `unsupported_claim_candidate` fixture's fabricated "seal was replaced yesterday" claim (known-issues.md ISSUE-003) — an existing, purpose-built example with no prior consumer.
- Ambiguous cases target the paraphrase/reasonable-inference-vs-unsupported-leap boundary.
- All cases hand-authored/synthetic, consistent with the `GS-DEBUG-TRAJ-01` precedent (Phase 4 Task 7).

### Test / Validation

- [ ] `groundedness_cases.json` contains exactly the manifest-declared cases (3 grounded / 3 unsupported / 3 ambiguous) and loads via the same fail-fast manifest pattern as the Phase 1/Phase 4 fixture loaders.
- [ ] The "clearly unsupported" set includes the ISSUE-003 seal-replacement case, with evidence text drawn from that fixture's actual tool outputs and answer text drawn from its actual fabricated claim.
- [ ] No pytest test anywhere in the repository asserts a pass/fail expectation against any case in this file — confirmed by code inspection.
- [ ] Running the notebook against a configured judge produces a printed verdict (`passed`/`score`/`reason`) for every case, without raising, for manual human review.

---

## Task 5 — `UncertaintyJudge`

### Failure taxonomy mapping — reuses `unsupported_claim`, no new type

- `UncertaintyJudge` emits `code="overconfident_hypothesis"`; this maps into the existing `unsupported_claim` failure type via a second entry in `FINDING_TO_FAILURE_TYPE` — the same many-to-one pattern already used throughout Phase 5's taxonomy (e.g. `tool_call_failed`/`tool_call_error` → `tool_failure`). Severity is already reserved as `error` for this type; no new severity work needed.
- Rejected alternative: introducing an eighth taxonomy value (e.g. `uncertainty_miscalibration`) with its own severity/priority/`CHECK` entries. Rejected as directly contradicting Phase 5 Task 4's explicit "frozen, not re-opened" framing, for a failure mode with no fixture-driven confirmation it needs its own bucket yet — the same category of speculative addition this project has declined elsewhere (e.g. ISSUE-004's deferred multi-value `secondary_category`).

### Scope — overconfidence only

- The judge checks specifically for a hypothesis/inference stated as settled fact without appropriate hedging — the safety-relevant direction, since a maintenance recommendation acted on with false certainty is the costly failure mode. No under-confidence/excessive-hedging check.
- Rejected alternative: a bidirectional judge also flagging unwarranted hedging on well-evidenced claims. Rejected given the plan's own stated priority ordering (uncertainty is the second-priority judge "if time becomes critical") and this project's consistent preference for narrow, single-purpose evaluator scope; a bidirectional judge would also have even less calibration coverage than a narrow one, given no calibration task was originally scoped for this judge at all (see below).

### Calibration fixture set (added during discussion, mirroring Task 4)

- Same shape as Task 4: minimal judge-scoped pairs, calling `UncertaintyJudge` directly.
- Same exercising mechanism as Task 4: notebook-only, no pytest assertions; Task 6's mocked-provider tests remain the only CI-covered mechanics check.
- ~6 cases: 3 clearly overconfident (a hypothesis stated as fact with no hedge, when evidence shows only one plausible contributing factor among several untested ones), 3 clearly appropriately-hedged (the same underlying evidence, correctly hedged). No ambiguous tier — the narrower overconfidence-only scope makes a meaningfully ambiguous case harder to construct than for groundedness, and none was asked for.
- Storage: `src/obs_platform/evaluation/judges/calibration/uncertainty_cases.json`, same manifest/loader convention.

### Everything else, matching `GroundednessJudge`'s pattern

- Class metadata: `name="uncertainty"`, `version="1.0.0"`, `type=EvaluatorType.LLM_BASED`.
- Not-applicable rule and evidence scope (`tool_calls.result` only, no `llm_calls`) identical to `GroundednessJudge`.
- Output schema: `passed: bool` (categorical), `score: float` (informational), `reason: str`, `overconfident_claims: list[OverconfidentClaim]` where `OverconfidentClaim = {claim_text, evidence_gap, explanation}`. `model_config = {"extra": "forbid"}`.

### Test / Validation

- [ ] `UncertaintyJudge` exposes `name="uncertainty"`/`version="1.0.0"`/`type=LLM_BASED`.
- [ ] `FINDING_TO_FAILURE_TYPE` contains `"overconfident_hypothesis" → unsupported_claim` as a second entry alongside `"unsupported_claim" → unsupported_claim` — confirmed no new failure type or severity table entry was introduced.
- [ ] `UncertaintyJudge`'s output schema has no field representing under-confidence/excessive hedging — confirmed by schema inspection.
- [ ] `uncertainty_cases.json` contains 3 clearly-overconfident and 3 clearly-appropriately-hedged cases, loaded via the same manifest pattern as `groundedness_cases.json`; no pytest assertion exists against it.
- [ ] Evidence scope (`tool_calls.result` only) and the not-applicable rule are implemented identically to `GroundednessJudge` — confirmed by code inspection (a shared helper, not two independent reimplementations).

---

## Task 6 — Structured judge outputs + bounded retry

### Retry scope — validation failures only, not transport errors

- The bounded retry loop (living in `JudgeClient`'s base-class `generate_structured()`, per Task 1) retries when the provider returns something that fails to parse into the target Pydantic schema, or declines to call the forced tool at all. Transport/API-level failures (timeouts, 5xx, rate limits, auth errors) are **not** retried — they propagate immediately, caught by Phase 5's existing per-evaluator orchestrator try/except and recorded as `status="failed"`.
- Rejected alternative: one unified retry budget covering both validation failures and transient transport errors. Rejected because it conflates two failure categories needing different recovery strategies (re-prompt-with-error vs. simple repeat) into one loop, and would blur the "provider outage produces failed/incomplete evaluation" boundary this phase's own constraints want kept sharp.

### Mechanics

- Retry budget: max 2 retries (3 attempts total).
- **Corrective retry**: each retry re-prompts with the prior invalid response's validation error appended, so the model can self-correct — mirroring the corrective-retry idiom Project 1's own `generate_structured()` already established for its forced-output path.
- "Invalid" is defined broadly: covers both schema-validation failure (`pydantic.ValidationError`) and the model declining to call the forced tool at all — both consume a retry.
- After the budget is exhausted, the client raises (`JudgeOutputValidationError`), uncaught — same "let it bubble, don't swallow" posture as every other evaluator (Phase 4 Task 1); Phase 5's orchestrator handles it exactly like any other evaluator exception.
- Every attempt — valid or invalid — is appended to `call_log` per Task 3's mechanism; no new plumbing needed here.
- Judge output schemas (`GroundednessJudgeOutput`, `UncertaintyJudgeOutput`) get `model_config = {"extra": "forbid"}`, mirroring Project 1's `WorkOrderDraft` precedent — a stray/injected field becomes an explicit validation error (and consumes a retry) rather than a silently-dropped no-op.

### Test / Validation

- [ ] A mocked provider returning schema-invalid JSON on the first two attempts and valid JSON on the third succeeds after exactly 2 retries (3 total `call_log` entries).
- [ ] A mocked provider returning schema-invalid JSON on all 3 attempts raises `JudgeOutputValidationError` after exactly 3 attempts, with 3 `call_log` entries persisted as `judge_calls` rows.
- [ ] A mocked provider raising a transport-level error (timeout/5xx) is not retried — confirmed exactly one `call_log` entry/`judge_calls` row exists, and the exception propagates immediately, resulting in `status="failed"`.
- [ ] Each retry's prompt includes the previous attempt's validation error text — confirmed by inspecting the constructed retry prompt in a test.
- [ ] `GroundednessJudgeOutput`/`UncertaintyJudgeOutput` both reject an unrecognized field with a validation error rather than silently dropping it.

---

## Task 7 — Skip/availability behavior when judge credentials/provider are absent

### Where and how skip is decided (applying an already-locked Phase 5 rule)

- The orchestrator checks judge availability (a cheap presence check on `JudgeSettings` — is an API key/model configured — not a live network ping) **before** invoking `evaluate_async()` on any `LLM_BASED` evaluator. If unconfigured, it records `status="skipped"` directly and never constructs a `JudgeClient` call, per Phase 5 Task 2's already-locked rule that "skip is an orchestrator decision made before calling `evaluate()`, never something an evaluator signals via its return value."
- `evaluation_results.status`'s `CHECK` constraint already includes `'skipped'` (Phase 5 Task 2) — no new migration needed.
- Distinct from a genuine outage: absent credentials → `SKIPPED` (this task); configured-but-provider-errored (retries exhausted, or a transport error) → `FAILED` (Task 6). The two conditions are non-overlapping and land in different statuses for different reasons.
- Global, not per-judge: Task 1 locked one shared `JudgeClient`/provider for both judges, so availability is a single check applied uniformly to every `LLM_BASED` evaluator in the registry.
- Explicitly out of scope: a feature-flag-style "disable `UncertaintyJudge` only, keep `GroundednessJudge` on" toggle — the task's trigger condition is specifically credential/provider absence, not an arbitrary enable/disable surface.

### `overall_status` is unaffected by `SKIPPED` — Phase 5 Task 3's rule is left untouched

- A run can be `overall_status="pass"` even when both LLM judges were skipped for missing credentials — behaves exactly as if the registry only ever had the five deterministic evaluators.
- Rejected alternative: amending Phase 5 Task 3 so `INCOMPLETE` also fires when any evaluator is `SKIPPED`. Rejected because it would directly undermine the design doc's explicit Tier A / "Core Platform Complete" guarantee — that checkpoint must be "independently runnable and portfolio-meaningful using only canonical mock telemetry... no LLM judge... required." Judge-less deployment (the anticipated default/CI case, not an edge case) would otherwise make `overall_status="incomplete"` and `behavioral_pass_rate=None` the universal result whenever judges aren't configured, breaking the platform's headline behavioral metric for exactly the deployment mode it was designed to support standalone.

### `SKIPPED` row contents and downstream effects

- Mirrors Phase 5 Task 2's already-decided `FAILED`-row treatment: `passed=NULL`, `score=NULL`, `label=NULL`, `severity=NULL`, `reason="judge credentials not configured"`, `findings=[{"code": "judge_unavailable", "message": ..., "data": {}}]` — one informational finding so a human reading `evaluation_summary` understands why groundedness/uncertainty are absent.
- No `judge_calls` row for a skipped evaluator — no call was ever attempted.
- `FailureClassifier` never treats a `SKIPPED` evaluator as a fail source — same treatment as `FAILED`, already covered by Phase 5 Task 5 Fork A's existing logic (only `COMPLETED` + `label="fail"` evaluators populate `primary_category`).
- Calibration notebooks (Tasks 4/5) check availability up front too, printing a clear "judge credentials not configured, skipping" message rather than crashing with an opaque provider-auth stack trace.

### Test / Validation

- [ ] With `JudgeSettings` unconfigured, the orchestrator never constructs a `JudgeClient` or attempts a provider call for any `LLM_BASED` evaluator — confirmed by asserting zero `judge_calls` rows are persisted for such a run.
- [ ] A run evaluated with judges unconfigured produces `evaluation_results` rows for both `GroundednessJudge`/`UncertaintyJudge` with `status="skipped"`, `passed`/`score`/`label`/`severity` all `NULL`, and exactly one `judge_unavailable` finding each.
- [ ] A run where all deterministic evaluators pass and both LLM-based evaluators are `SKIPPED` produces `overall_status="pass"` — confirmed against Phase 5 Task 3's unmodified rule.
- [ ] `primary_category`/`secondary_category`/`max_severity` are unaffected by a `SKIPPED` evaluator's presence — confirmed a skipped-only run never populates them, same as a clean pass.
- [ ] The full deterministic + Phase 4/5 test suite still passes unmodified with judge credentials absent (the default CI state), confirming the Tier A / Core Platform Complete guarantee holds with Phase 6 code present but unconfigured.

---

## Success Criteria

- [ ] Both LLM-based judges (`GroundednessJudge`, `UncertaintyJudge`) plug into the existing evaluator registry/orchestrator via a dual sync/async interface that leaves all five Phase 4 evaluators completely untouched, using the `type` field for exactly the purpose Phase 4 Task 1 reserved it for (Task 2).
- [ ] A single, provider-agnostic `JudgeClient` abstraction (ABC template method) centralizes instrumentation and retry logic so both judges share identical accounting/retry behavior with zero duplicated code, and adding a second provider later touches only one method per adapter (Task 1, Task 6).
- [ ] Judge cost/latency/token usage is persisted with full per-attempt fidelity — including failed/retried attempts — in a table structurally and accounting-wise separate from the agent's own `llm_calls`, closing the gap Phase 5 Task 9 explicitly deferred to this phase (Task 3).
- [ ] Both judges' findings resolve into the existing, frozen seven-value failure taxonomy with no new types or severities invented — `unsupported_claim` absorbs both groundedness and uncertainty findings via distinct finding codes, the same many-to-one pattern already used throughout Phase 5's taxonomy (Task 2, Task 5).
- [ ] A small, human-audited calibration set exists for each judge, deliberately kept out of the automated pytest suite — Task 6's mocked-provider mechanics tests cover retry/schema behavior instead — so "does this look right to a person" and "does the code work" stay two distinct, non-duplicated concerns (Task 4, Task 5).
- [ ] Bounded retry recovers from invalid structured output specifically, not transport failures, preserving the existing "provider outage produces failed/incomplete evaluation, not a masked retry" boundary (Task 6).
- [ ] The platform's Tier A / Core Platform Complete guarantee — fully functional deterministic evaluation with zero LLM judge dependency — is verified to still hold with Phase 6 code present: missing judge credentials skip cleanly, and `overall_status`/`behavioral_pass_rate` are computed exactly as Phase 5 defined them, unaffected by judge availability (Task 7).

## Status

All seven Phase 6 tasks are locked. Phase 6 planning is complete. `FINDING_TO_FAILURE_TYPE` (Phase 5 Task 5) is extended with two new entries (`unsupported_claim` and `overconfident_hypothesis`, both resolving to the existing `unsupported_claim` failure type) — additive only, no taxonomy amendment. No prior "locked" decision from Phases 0–5 is contradicted; Task 2's dual sync/async orchestrator branch is new work fulfilling what Phase 4 Task 1 explicitly anticipated needing ("an accepted, foreseeable... change Phase 6 will need to make"). Next: proceed to implementation, or move on to Phase 7 (Regression Evaluation) planning discussion.