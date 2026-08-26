# Telemetry v1 Fixtures

Each JSON file is a canonical, human-auditable run-level telemetry snapshot.

- `healthy_success`: clean baseline for a successful inspection of `PUMP-101`.
- `tool_failure`: fatal `get_asset_status` failure; the only `tool_error` fixture.
- `trajectory_error`: successful runtime trace with a work-order draft created before evidence gathering.
- `retrieval_failure`: successful degraded answer after `search_maintenance_docs` returns no documents.
- `unsupported_claim_candidate`: successful trace whose final answer includes a claim not grounded in captured evidence.
- `policy_violation`: intentionally adversarial trace where `submit_work_order` appears without prior draft approval.
- `hitl_pending`: `GS-08` run paused for human approval with pending high-priority draft details.
- `hitl_approved`: resumed `GS-08` run with all prior child IDs carried forward and a new submit action.
