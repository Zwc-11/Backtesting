"""Read-only dashboard API; raw data and credentials are never publicly mounted."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from xasset.monitor.lab_api import mount as mount_lab


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def app(root: Path) -> FastAPI:
    api = FastAPI(
        title="xasset observation dashboard", docs_url=None, redoc_url=None, openapi_url=None
    )
    web = Path(__file__).resolve().parents[1] / "web"
    token = os.getenv("XASSET_DASHBOARD_TOKEN")

    def authorized(value: str | None) -> bool:
        return not token or (value is not None and secrets.compare_digest(value, token))

    @api.middleware("http")
    async def protect(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not authorized(request.cookies.get("xasset_session") or bearer):
                return JSONResponse({"error": "Authentication required"}, status_code=401)
        response = await call_next(request)
        if request.url.path.startswith("/assets/fonts/"):
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    class Login(BaseModel):
        token: str

    @api.post("/api/session")
    def login(body: Login, request: Request) -> JSONResponse:
        if not authorized(body.token):
            raise HTTPException(401, "Incorrect access token")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "xasset_session",
            body.token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=3600,
        )
        return response

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(web / "index.html")

    @api.get("/assets/{name}")
    def asset(name: str) -> FileResponse:
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(web / name)

    fonts = {path.name for path in (web / "fonts").glob("*.woff2")}

    @api.get("/assets/fonts/{name}")
    def font(name: str) -> FileResponse:
        if name not in fonts:
            raise HTTPException(404)
        response = FileResponse(web / "fonts" / name, media_type="font/woff2")
        return response

    mount_lab(api, root)

    @api.get("/healthz")
    def liveness() -> dict[str, bool]:
        return {"ok": True}

    @api.get("/api/summary")
    def summary() -> dict[str, Any]:
        monitor = read_json(root / "monitor/latest.json", {})
        mapping = read_json(root / "maps/latest.json", {})
        campaign = read_json(root / "reports/completion/campaign-status.json", {})
        return {
            "monitor": monitor,
            "map": {
                key: mapping.get(key)
                for key in (
                    "id",
                    "generated_at",
                    "registered_cells",
                    "counts",
                    "tradable",
                    "limitations",
                )
            },
            "campaign": campaign,
            "orders_enabled": False,
            "accepted_strategies": 0,
        }

    @api.get("/api/map")
    def mapping(horizon: str = "5m", kind: str = "all", state: str = "stable") -> dict[str, Any]:
        data = read_json(root / "maps/latest.json", {})
        edges = [
            e
            for e in data.get("edges", [])
            if e["horizon"] == horizon
            and (kind == "all" or e["kind"] == kind)
            and (state == "all" or e["status"] == state)
        ]
        edges.sort(
            key=lambda e: (e["q_validation"], e["q_train"], -abs(e["train"]["strength"] or 0))
        )
        return {
            "nodes": data.get("nodes", []),
            "edges": edges[:400],
            "total": len(edges),
            "design": data.get("design"),
            "limitations": data.get("limitations", []),
        }

    @api.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "live": read_json(root / "monitor/latest.json", {}).get("health", []),
            "equities": read_json(root / "reports/completion/sip-audit.json", {}).get(
                "symbols", []
            ),
            "readiness": read_json(root / "reports/completion/sip-backfill.json", {}).get(
                "readiness", []
            ),
        }

    @api.get("/api/experiments")
    def experiments() -> Any:
        return read_json(root / "reports/completion/campaign-status.json", {"cases": []})

    @api.websocket("/ws")
    async def stream(socket: WebSocket) -> None:
        if not authorized(socket.cookies.get("xasset_session")):
            await socket.close(code=1008)
            return
        # Cookie authentication also requires a same-host Origin for browsers.
        origin = socket.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != socket.headers.get("host"):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            while True:
                await socket.send_json(read_json(root / "monitor/latest.json", {}))
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            return

    return api
