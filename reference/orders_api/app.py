"""Reference orders-api application with Prometheus metrics and registered faults."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from faults import apply_fault, get_fault, reset_fault
from metrics import metrics_payload, observe_db, observe_request
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
                for i in range(1, 8):
                    order = Order(
                        customer_id=1 if i % 2 else 2,
                        status="created",
                        total_cents=i * 1000,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    db.add(order)
                    db.commit()
                    db.refresh(order)
                    db.add(OrderItem(order_id=order.id, sku=f"SKU-{i}", qty=1, price_cents=1000))
                    db.commit()
        finally:
            db.close()

    seed()

    @app.middleware("http")
    async def _metrics_middleware(request, call_next):
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            observe_request(request.url.path, request.method, status, time.perf_counter() - started)

    @app.get("/health")
    def health() -> dict[str, str]:
        fault = get_fault()
        return {"status": "ok", "fault_type": fault.get("fault_type") or "", "case_id": fault.get("case_id") or ""}

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)

    @app.get("/metrics/json")
    def metrics_json() -> dict[str, Any]:
        fault = get_fault()
        return {
            "service": "orders-api",
            "fault_type": fault.get("fault_type"),
            "case_id": fault.get("case_id"),
            "source": "prometheus_client_registry",
        }

    @app.post("/admin/fault")
    def set_fault(body: dict[str, Any]) -> dict[str, Any]:
        fault_type = body.get("fault_type")
        if not fault_type:
            raise HTTPException(status_code=400, detail="fault_type required")
        try:
            return apply_fault(fault_type, body.get("params") or {}, body.get("case_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/admin/fault/reset")
    def reset() -> dict[str, str]:
        reset_fault()
        return {"status": "reset"}

    def _load_orders(db: Session, limit: int) -> list[OrderOut]:
        fault = get_fault()
        ftype = fault.get("fault_type")
        params = fault.get("params") or {}
        db_started = time.perf_counter()
        orders = db.execute(select(Order).limit(limit)).scalars().all()
        observe_db("select_orders", time.perf_counter() - db_started)

        if ftype in {"slow_database_query", "dependency_timeout", "connection_pool_exhaustion"}:
            time.sleep(float(params.get("delay_seconds", 0.5)))
        if ftype == "null_exception":
            raise RuntimeError(params.get("error_message", "null pointer in order serializer"))
        if ftype == "incorrect_configuration":
            # pathological tiny page
            orders = orders[: int(params.get("max_limit", 1))]
        if ftype == "schema_mismatch":
            # simulate missing field error
            _ = params.get("missing_field", "total_cents")

        result: list[OrderOut] = []
        for order in orders:
            q_started = time.perf_counter()
            items = [
                {"sku": it.sku, "qty": it.qty, "price_cents": it.price_cents}
                for it in db.execute(
                    select(OrderItem).where(OrderItem.order_id == order.id)
                ).scalars()
            ]
            observe_db("select_items_per_order", time.perf_counter() - q_started)
            if ftype in {"n_plus_one_query", "missing_index", "bad_query_refactor", "cache_failure"}:
                time.sleep(float(params.get("per_row_delay", 0.03)))
            if ftype == "schema_mismatch":
                raise KeyError(f"missing field {params.get('missing_field')}")
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

    @app.get("/orders", response_model=list[OrderOut])
    def list_orders(limit: int = Query(default=50, le=200)) -> list[OrderOut]:
        db = get_db()
        try:
            return _load_orders(db, limit)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                json.dumps(
                    {
                        "event": "orders_list_error",
                        "fault_type": get_fault().get("fault_type"),
                        "case_id": get_fault().get("case_id"),
                        "error_type": type(exc).__name__,
                        "request_id": str(uuid.uuid4()),
                    }
                )
            )
            raise HTTPException(status_code=500, detail="orders listing failed") from None
        finally:
            db.close()

    @app.get("/orders/{order_id}", response_model=OrderOut)
    def get_order(order_id: int) -> OrderOut:
        db = get_db()
        try:
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
        finally:
            db.close()

    return app


app = create_app()
