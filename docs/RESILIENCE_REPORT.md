# IncidentPilot Resilience Report

This report separates verified automated behavior from experiments that have not run. It contains no production traffic and no invented measurements. Machine-readable status is in `load/results/resilience-results.json`.

Every measurement is attributed to the host and date it was taken, because they are not the
same machine: the 2026-09-18 rows ran on a Windows host with standalone `docker-compose`, and
the 2026-11-03 rows ran on macOS with the Compose plugin plus GitHub-hosted `ubuntu-latest` CI.

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
- An approval decision is executed by a leased worker after the run is re-queued, not inside the HTTP request, so a disconnected client cannot cancel the resumed graph or the GitHub call it makes.
- Graph resume is bounded by a timeout, and a heartbeat survives a transient database error instead of abandoning a live lease (`tests/test_approval_handoff.py` 3 tests, `tests/test_worker_resilience.py` 9 tests).
- Queue and lease gauges are published only by the process that owns them, and with no port configured the worker binds nothing at all (`tests/test_metrics_export.py`, 2 tests).
- The reference Git fixture produces a repository whose HEAD tree equals the deployed source, and refuses to write into a destination owned by another uid rather than destroying a working repository first (`tests/test_reference_git_fixture.py`, 3 tests).
- Outside `development` the schema is Alembic-only: the empty-database gate asserts zero tables, so an auto-`create_all` path fails the suite instead of silently diverging from migrations.
- `read_source_code` returns one line-aligned window near 3,200 characters with `start_line`/`end_line`/`total_lines`/`has_more`, so a file larger than the Agent's 4,000-character projection cap can still be read to its end; the paged test walks `reference/orders_api/app.py` and asserts the window containing the injected loop is reachable (`tests/test_phase4_mcp.py`). `has_more`, not `truncated`, is the field name because the gateway rejects truncated payloads as incomplete.
- An over-budget context envelope drops the oldest evidence entries and records `omitted_items` instead of replacing every observation with an excerpt of its own JSON (`tests/test_audit_tools.py`).
- Structured-output repair tells the model which field failed and shows the rejected output; measured against deepseek-flash, a blind re-ask silently replaced two intended observations with different tool calls (`tests/test_audit_reliability.py`).
- The evaluator's `injection_hit` assertion requires at least one conclusion citation to contain the fault case's `injection_evidence` markers in the injected file; valid citations to agreeing telemetry alone do not satisfy it (`tests/test_audit_tools.py`).

## Experiment status — 2026-11-03 (macOS host, GitHub-hosted ubuntu-latest)

| Experiment | Status | Measurements |
|---|---|---|
| Full test suite in CI | Executed | `219 passed, 3 skipped, 55 warnings in 162.25s` (run 41574057091, commit `f58ff51`). The 3 skips are environmental: 2 require `os.name == "nt"` (Windows `tasklist`), 1 requires `LLM_API_KEY`. |
| Full test suite on macOS | Executed | `219 passed, 3 skipped in 118.15s` after this report was refreshed; `219 passed, 3 skipped in 119.63s` with the reference role URLs configured earlier the same day, and `218 passed, 4 skipped in 122.00s` without them. |
| SQL least-privilege role check in CI | Executed | Same commit, before/after the fixture: run 41572899582 `218 passed, 4 skipped` → run 41574057091 `219 passed, 3 skipped`. The check can fail: granting `INSERT` to `reference_readonly` produces `database role has write privileges`, and denying it login produces a connection refusal — both verified on `postgres:16-alpine`. |
| PostgreSQL Alembic upgrade | Executed | CI step against a fresh `postgres:16-alpine` service, head `0005_security_bindings`. |
| Containerized remediation loop (live Compose stack) | Executed | Measured before 03:08:06Z (commit `39d2053`). Queued run → live MCP metrics and logs → `inspect_git_history` on the fixture repository → patch → nested sandbox container pytest `PASSED`, exit 0 → `WAITING_APPROVAL` → APPROVE → re-queue → `CREATE_PULL_REQUEST` → `RESOLVED` with the mock PR. `base_commit_sha` was a real 40-hex HEAD, not `UNVERSIONED`. **No fault was injected, so this proves the pipeline runs, not that the agent diagnoses correctly.** |
| Worker queue metrics scraped from the owning process | Executed | Measured before 02:31:25Z (commit `f6011df`). Three queued runs reported `incidentpilot_queue_depth 3` / `incidentpilot_active_runs 2` on the worker target while the API process reported `0` for the same gauges. |
| Base and chaos Compose validation | Executed | `docker compose config --quiet` and `docker compose -f docker-compose.yml -f docker-compose.chaos.yml config --quiet` both exit 0 on macOS at 06:33:03Z. The gate runs in CI on every push (`.github/workflows/ci.yml`, added with commit `5c3b286`); the macOS invocation above is the last recorded local measurement of it. |
| Real-model investigation of an injected fault (deepseek-flash) | Executed | Run `RUN-3232bb93aa4b` on 2026-11-03, DeepSeek `deepseek-flash`, live MCP tools against the reference stack, `real_llm_investigation` tier, ground truth withheld. Reached `INVESTIGATION_COMPLETE` in 113.6s with 7 tool calls (metrics, git history, three source windows, logs, faults.py) and 7 evidence items; tokens `54,450 / 15,266 / 69,716` (prompt/completion/total). `evidence_bound` true, component correct, causal facts matched, and the new `injection_hit` assertion true: the cited `EVID-5f4d020a0a9e` is the `app.py` window containing `for order in orders`, `OrderItem.order_id == order.id` and `select_items_per_order`. The agent still answered `application_bug` against the declared `database_performance` category, so `root_cause_correct` is false — the loop is grounded in the injected site, but the diagnosis hedged across fault sub-types. |

## Experiment status — 2026-09-18 (Windows host, standalone `docker-compose` v5.1.4)

| Experiment | Status | Measurements |
|---|---|---|
| Automated resilience/full test suite | Executed | `78 passed, 1 skipped (LLM_API_KEY), 1 warning` in 76.55s — superseded by the 2026-11-03 rows above. |
| Empty-database Alembic upgrade (SQLite) | Executed | `0004_worker_leases (head)` at that date; the current head is `0005_security_bindings`. |
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

The 2026-09-18 experiments ran on a Windows host where the Compose CLI is standalone
`docker-compose` v5.1.4 and the `docker compose` plugin is unavailable; on macOS and in CI the
`docker compose` plugin is used. Both spellings address the same two files, and CI now
validates the merged configuration rather than only the base file.

Run synthetic load with `uv run --with locust locust -f load/locustfile.py --host http://127.0.0.1:8000`. Start controlled proxies and two workers with `docker compose -f docker-compose.yml -f docker-compose.chaos.yml up`. Clean up with the matching `down`; no experiment targets real GitHub by default.

## Honesty boundaries

- No real production traffic was measured.
- No real GitHub Pull Requests were created.
- The deepseek-flash row is a single run of a single fault case, not an accuracy benchmark;
  it shows the investigation loop can complete and be grounded in the injected code, not
  that the model diagnoses every case. FakeModel evidence cannot speak to whether a real
  model would be semantically misled by injected content.
- Chaos numbers are local synthetic measurements, not enterprise SLOs.
- The containerized remediation loop ran **without an injected fault**: it demonstrates that
  evidence collection, patching, testing, approval and the PR step all execute, not that the
  agent identifies a real cause correctly.
- The reference repository built by `incidentpilot-refgit` is a reconstruction of the deployed
  tree in two commits, not the project's deployment history, and the injected faults are
  in-process state in `faults.py` that no commit describes.
