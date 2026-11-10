"""Liveness is separate from readiness and operational metrics."""

import time
from aiohttp import web
from sqlalchemy import text, select, func
from db.core import get_session
from db.models import UpdateInbox, SendJob, PaymentOrder

STARTED = time.monotonic()


def add_health_routes(app, manager):
    async def live(request):
        return web.json_response(
            {"status": "ok", "uptime_seconds": int(time.monotonic() - STARTED)}
        )

    async def ready(request):
        try:
            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            loading = manager is not None and (
                manager._loading or not getattr(manager, "_ready", True)
            )
            return web.json_response(
                {"status": "loading" if loading else "ready"},
                status=503 if loading else 200,
            )
        except Exception:
            return web.json_response({"status": "database_unavailable"}, status=503)

    async def metrics(request):
        from services.security import internal_authorized

        if not internal_authorized(request):
            return web.Response(status=403)
        values = {}
        async with get_session() as session:
            for name, model, column in [
                ("inbox", UpdateInbox, UpdateInbox.state),
                ("send_jobs", SendJob, SendJob.state),
                ("orders", PaymentOrder, PaymentOrder.status),
            ]:
                rows = await session.execute(
                    select(column, func.count()).group_by(column)
                )
                values[name] = dict(rows.all())
        from send_queue import _queues

        values["bots"] = manager.active_count if manager else 0
        values["pending_in_memory"] = sum(q.pending for q in _queues.values())
        return web.json_response(values)

    app.router.add_get("/health", live)
    app.router.add_get("/ready", ready)
    app.router.add_get("/internal/metrics", metrics)
