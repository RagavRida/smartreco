"""Convenience entry point: `python3 run.py` (equivalent to uvicorn app.main:app)."""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "true").lower() in {"1", "true", "yes"},
    )
