"""FastAPI application entrypoint for the Context Memory backend."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from context_memory.db import init_db
from context_memory.routers import ai, items, review


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Create database tables on startup."""
    init_db()
    yield


app = FastAPI(title="Context Memory API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(items.router, prefix="/api")
app.include_router(review.router, prefix="/api")
app.include_router(ai.router, prefix="/api")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok"}
