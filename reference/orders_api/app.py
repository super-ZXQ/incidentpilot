"""Reference orders-api application (controlled fault experiment service)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

logger = logging.getLogger("orders_api")


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    sku: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    price_cents: Mapped[int] = mapped_column(Integer, default=100)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(64), default="")


class OrderOut(BaseModel):
    id: int
    customer_id: int
    status: str
    total_cents: int
    created_at: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class Metrics:
    def __init__(self) -> None:
        self.request_count = 0
        self.error_count = 0
        self.latencies_ms: list[float] = []

    def observe(self, latency_ms: float, error: bool = False) -> None:
        self.request_count += 1
        self.latencies_ms.append(latency_ms)
        if error:
            self.error_count += 1

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0.0
        error_rate = (self.error_count / self.request_count) if self.request_count else 0.0
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "error_rate": round(error_rate, 4),
            "p95_latency_ms": round(p95, 2),
        }


METRICS = Metrics()

# Fault modes injected by Fault Injector (registered, not arbitrary shell)
FAULT_MODE: dict[str, Any] = {"fault_type": None, "params": {}}


def reset_fault() -> None:
    FAULT_MODE["fault_type"] = None
    FAULT_MODE["params"] = {}
    METRICS.__init__()


def apply_fault(fault_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Registered fault handler used by Fault Injector."""
    if fault_type not in {
        "slow_database_query",
        "missing_index",
        "n_plus_one_query",
        "bad_code_commit",
        "null_exception",
        "dependency_timeout",
        "connection_pool_exhaustion",
        "schema_mismatch",
        "cache_failure",
        "incorrect_configuration",
    }:
        raise ValueError(f"unknown fault_type: {fault_type}")
    FAULT_MODE["fault_type"] = fault_type
    FAULT_MODE["params"] = params or {}
    logger.info(json.dumps({"event": "fault_applied", "fault_type": fault_type, "params": params}))
    return {"fault_type": fault_type, "params": params or {}}


def create_app(database_url: str = "sqlite:///:memory:") -> FastAPI:
    app = FastAPI(title="orders-api", version="0.1.0")
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if ":memory:" in database_url:
        kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(engine)

    def get_db() -> Session:
        return Session(engine)

    def seed() -> None:
        db = get_db()
        try:
            if db.execute(select(Customer)).first() is None:
                db.add_all([Customer(name="alice"), Customer(name="bob")])
                db.commit()
                for i in range(1, 6):
                    order = Order(
                        customer_id=1 if i % 2 else 2,
                        status="created",
                        total_cents=0,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    db.add(order)
                    db.commit()
                    db.refresh(order)
                    item = OrderItem(order_id=order.id, sku=f"SKU-{i}", qty=1, price_cents=1000)
                    db.add(item)
                    db.commit()
        finally:
            db.close()

    seed()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "fault_type": FAULT_MODE.get("fault_type") or ""}

    @app.post("/admin/fault")
    def set_fault(body: dict[str, Any]) -> dict[str, Any]:
        fault_type = body.get("fault_type")
        if not fault_type:
            raise HTTPException(status_code=400, detail="fault_type required")
        return apply_fault(fault_type, body.get("params") or {})

    @app.post("/admin/fault/reset")
    def reset() -> dict[str, str]:
        reset_fault()
        return {"status": "reset"}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return {"service": "orders-api", **METRICS.snapshot(), "fault_type": FAULT_MODE.get("fault_type")}

    @app.get("/orders", response_model=list[OrderOut])
    def list_orders(limit: int = Query(default=50, le=200)) -> list[OrderOut]:
        started = time.perf_counter()
        error = False
        try:
            db = get_db()
            orders = db.execute(select(Order).limit(limit)).scalars().all()
            fault = FAULT_MODE.get("fault_type")
            result: list[OrderOut] = []

            if fault in {"slow_database_query", "dependency_timeout"}:
                delay = float(FAULT_MODE.get("params", {}).get("delay_seconds", 0.8))
                time.sleep(delay)
            if fault == "null_exception":
                raise RuntimeError("null pointer in order serializer")

            for order in orders:
                items = [
                    {"sku": it.sku, "qty": it.qty, "price_cents": it.price_cents}
                    for it in db.execute(
                        select(OrderItem).where(OrderItem.order_id == order.id)
                    ).scalars()
                ]
                # N+1 fault: extra sleep per order when injected
                if fault == "n_plus_one_query":
                    time.sleep(float(FAULT_MODE.get("params", {}).get("per_row_delay", 0.05)))
                result.append(
                    OrderOut(
                        id=order.id,
                        customer_id=order.customer_id,
                        status=order.status,
                        total_cents=order.total_cents,
                        created_at=order.created_at,
                        items=items,
                    )
                )
            return result
        except Exception:
            error = True
            logger.exception(json.dumps({"event": "orders_list_error", "fault_type": FAULT_MODE.get("fault_type")}))
            raise HTTPException(status_code=500, detail="orders listing failed") from None
        finally:
            latency = (time.perf_counter() - started) * 1000
            METRICS.observe(latency, error=error)
            logger.info(
                json.dumps(
                    {
                        "event": "orders_list",
                        "latency_ms": round(latency, 2),
                        "error": error,
                        "fault_type": FAULT_MODE.get("fault_type"),
                        "request_id": str(uuid.uuid4()),
                    }
                )
            )

    @app.get("/orders/{order_id}", response_model=OrderOut)
    def get_order(order_id: int) -> OrderOut:
        db = get_db()
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        items = [
            {"sku": it.sku, "qty": it.qty, "price_cents": it.price_cents}
            for it in db.execute(select(OrderItem).where(OrderItem.order_id == order.id)).scalars()
        ]
        return OrderOut(
            id=order.id,
            customer_id=order.customer_id,
            status=order.status,
            total_cents=order.total_cents,
            created_at=order.created_at,
            items=items,
        )

    return app


app = create_app()
