"""Synthetic API load only; this does not represent production traffic."""

from __future__ import annotations

import random
import uuid

from locust import HttpUser, between, task


class IncidentPilotUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self) -> None:
        self.run_ids: list[str] = []

    @task(3)
    def create_incident(self) -> None:
        with self.client.post(
            "/v1/incidents",
            json={
                "incident_id": f"LOAD-{uuid.uuid4().hex}",
                "title": "synthetic orders latency",
                "service": "orders-api",
                "symptom": "synthetic P95 latency increase",
                "severity": "SEV3",
            },
            name="POST /v1/incidents",
            catch_response=True,
        ) as response:
            if response.status_code == 202:
                self.run_ids.append(response.json()["run_id"])
                response.success()
            elif response.status_code == 429:
                # Expected under queue backpressure
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")

    @task(5)
    def poll_run(self) -> None:
        if self.run_ids:
            run_id = random.choice(self.run_ids)
            self.client.get(f"/v1/runs/{run_id}", name="GET /v1/runs/:id")

    @task(1)
    def concurrent_approval_candidate(self) -> None:
        if not self.run_ids:
            return
        run_id = random.choice(self.run_ids)
        with self.client.post(
            f"/v1/runs/{run_id}/approval",
            json={"decision": "APPROVE", "actor": "synthetic-load"},
            name="POST /v1/runs/:id/approval",
            catch_response=True,
        ) as response:
            if response.status_code in {200, 409}:
                response.success()
            else:
                response.failure(f"unexpected status {response.status_code}")

