"""The durable worker's gauges only mean something on the process that sets them."""

from __future__ import annotations

import socket
import urllib.request

from incidentpilot.config import Settings
from incidentpilot.observability.metrics import (
    QUEUE_DEPTH,
    start_metrics_exporter,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_no_port_means_no_listener() -> None:
    """An unauthenticated metrics endpoint must not open by default."""
    assert start_metrics_exporter(Settings(worker_metrics_port=0)) is None


def test_exporter_serves_the_queue_depth_this_process_set() -> None:
    port = _free_port()
    observed = 17.0
    previous = QUEUE_DEPTH._value.get()
    QUEUE_DEPTH.set(observed)
    exporter = start_metrics_exporter(
        Settings(worker_metrics_port=port, worker_metrics_address="127.0.0.1")
    )
    assert exporter is not None
    httpd, _thread = exporter
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            body = r.read().decode()
        assert r.status == 200
        assert f"incidentpilot_queue_depth {observed}" in body
        # The lease counters live here too; the API process can only report zeros.
        assert "incidentpilot_lease_expirations_total" in body
    finally:
        httpd.shutdown()
        QUEUE_DEPTH.set(previous)
