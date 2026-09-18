# ADR-012: PostgreSQL worker leases and at-least-once execution

Status: Accepted

## Context

IncidentPilot needs bounded concurrent workers, crash recovery, and multi-instance task claiming.
The control-plane state and LangGraph checkpoints already live in PostgreSQL. Adding a second queue
system would create another consistency boundary without a demonstrated throughput need.

## Decision

`AgentRun` is also the durable job record. A worker atomically claims one eligible run with
`SELECT ... FOR UPDATE SKIP LOCKED`, writes `worker_id`, increments `attempt_count`, and sets
`lease_expires_at` and `last_heartbeat_at`. A live worker renews the lease periodically. A different
worker may claim a `RUNNING` run only after its lease expires and then resumes the existing
LangGraph thread/checkpoint.

The API only persists a `PENDING` run in production mode. A small, explicitly configured embedded
executor remains available for deterministic tests and local development.

Execution is **at least once**. PostgreSQL prevents simultaneous ownership, but a process can die
after an external effect succeeds and before local state commits. Therefore external effects use a
stable idempotency key and a durable `external_side_effects` record. GitHub recovery also checks the
deterministic branch/PR before attempting creation again.

Expired leases are recoverable until `MAX_JOB_ATTEMPTS`; exhausted jobs become
`NEEDS_HUMAN_INTERVENTION`. Queue admission is serialized in PostgreSQL and bounded by
`MAX_QUEUED_RUNS`.

## Consequences

- No Redis, Celery, Kafka, or new operational datastore is required.
- PostgreSQL is a coordination bottleneck by design; polling and row locking are appropriate for
  the portfolio/reference workload.
- This supports a small number of worker processes, not unlimited horizontal scale.
- Long workflow nodes must remain below the lease duration or maintain heartbeats.
- Correctness of remote effects depends on stable idempotency keys and provider-side lookup.
