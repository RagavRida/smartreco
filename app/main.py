"""SmartReco — FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, settings
from .database import init_db
from .deps import RedirectToLogin, redirect, render
from .routers import admin, auth, catalog, events, recommendations
from .services import scheduler, tracing, vector_store
from .agent.graph import engine_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
logger = logging.getLogger("smartreco")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tracing.setup()
    store = vector_store.get_store()
    logger.info(
        "SmartReco up — vector_backend=%s agent_engine=%s mesh_configured=%s",
        store.backend, engine_name(), settings.mesh_configured,
    )
    if not settings.mesh_configured:
        logger.warning(
            "MESH_API_KEY is not set. The app runs, but AI copy falls back to templates. "
            "Add your rsk_... key to .env to enable the agent."
        )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="SmartReco", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(admin.router)
app.include_router(events.router)
app.include_router(recommendations.router)


@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(request: Request, exc: RedirectToLogin):
    return redirect(f"/login?next={exc.next_url}")


@app.exception_handler(404)
async def _not_found(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return render(request, "404.html", {"current_user": None}, status_code=404)


@app.get("/api/health", tags=["system"])
def health():
    return {
        "status": "ok",
        "mesh_configured": settings.mesh_configured,
        "vector_backend": vector_store.get_store().backend,
        "vector_documents": vector_store.get_store().count(),
        "agent_engine": engine_name(),
        "scheduler": scheduler.status(),
        "tracing": tracing.is_enabled(),
    }
