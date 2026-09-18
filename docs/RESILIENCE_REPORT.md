# IncidentPilot Resilience Report

This report separates verified automated behavior from experiments that have not run. It contains no production traffic and no invented measurements. Machine-readable status is in `load/results/resilience-results.json`.

## Verified by automated tests

- A live lease cannot be claimed by a second worker; an expired lease can be recovered.
- Attempts are bounded and exhaustion transitions to `NEEDS_HUMAN_INTERVENTION`.
- Queue admission returns HTTP 429 with `RUN_QUEUE_FULL` at the configured bound.
- Concurrent approval has one database winner and the GitHub mock records exactly one PR creation.
- Tool timeouts, connection resets, 429 and retryable 5xx are transient categories; validation and permission failures are permanent.
- Evidence and PatchArtifact persistence is replay-safe by stable identifiers/content identity.
- Windows worker entry points use a Selector event loop, required by async psycopg.
- Stale workers cannot renew a lease after another worker takes ownership.
- Approval persisted before graph resume remains recoverable after worker crash.
- P1 tests cover heartbeat ownership loss, approval-crash recovery, evidence replay, GitHub mock idempotency, and attempt increment on crash recovery.

## Experiment status

| Experiment | Status | Measurements |
|---|---|---|
| Automated resilience/full test suite | Executed | `78 passed, 1 skipped (LLM_API_KEY), 1 warning` in 76.55s |
| Empty-database Alembic upgrade (SQLite) | Executed | `0004_worker_leases (head)` |
| PostgreSQL Alembic upgrade | Executed | `0004_worker_leases (head)` dialect `PostgresqlImpl` |
| Combined base/chaos Compose validation | Executed | PASS via `docker-compose -f docker-compose.yml -f docker-compose.chaos.yml config --quiet` |
| Toxiproxy image pin | Executed | `ghcr.io/shopify/toxiproxy:2.9.0` (Docker Hub `shopify/toxiproxy:2.9.0` does not exist) |
| Local two-worker PostgreSQL competition | Executed | Two concurrent claimers, exactly one successful claim |
| Worker process termination and lease recovery | Executed | Attempt 1→2; replacement reached `WAITING_APPROVAL`; GitHub disabled |
| Dual OS worker process claim | Executed | Two real `python -m incidentpilot.worker` processes; run `RUN-f99a01f758e8` reached `WAITING_APPROVAL` with `attempt_count=1` (single owner) |
| MCP stdio live protocol | Executed | 1 passed |
| Docker sandbox live isolation | Executed | 2 passed |
| Reference fault lifecycle | Executed | 11 passed |
| PostgreSQL latency / disconnect through Toxiproxy | Executed | Proxy `127.0.0.1:15432`; toxic latency 400ms applied and removed; queries 5/5/5 ok; avg ms baseline 2.51 → degraded 3.41 → recovered 4.18 |
| MCP timeout / reset through Toxiproxy | Executed | Direct avg ~1.05–1.36s; proxy+latency ~1.75–1.84s; timeout toxic caused 2 disconnects; recovery 3/3 ok; Tool Gateway SUCCEEDED after 3 attempts with delays `[0.125, 0.25]` |
| GitHub 429 / 500 | Executed (mock/stub only) | Idempotency key: create count=1; crash-after-create replay count=1; **real GitHub not called** |
| Locust concurrent create/poll/approve/backpressure | Not executed | `load/locustfile.py` ready; local uvicorn factory startup failed on this host; **no production traffic claimed** |

## Chaos compose note

On this Windows host the Compose CLI is `docker-compose` (standalone v5.1.4); `docker compose` plugin is not available. Toxiproxy runs from the GHCR pin above.

Run synthetic load with `uv run --with locust locust -f load/locustfile.py --host http://127.0.0.1:8000`. Start controlled proxies and two workers with `docker-compose -f docker-compose.yml -f docker-compose.chaos.yml up`. Clean up with the matching `down`; no experiment targets real GitHub by default.

## Honesty boundaries

- No real production traffic was measured.
- No real GitHub Pull Requests were created.
- No real LLM benchmark is claimed in this report without credentials.
- Chaos numbers are local synthetic measurements, not enterprise SLOs.
