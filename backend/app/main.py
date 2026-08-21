"""Note Insight FastAPI application entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import analysis, auth, notes

settings = get_settings()

app = FastAPI(
    title="Note Insight API",
    version="0.1.0",
    description="Clinical note analysis API",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(analysis.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe; does not require authentication."""
    return {"status": "ok"}
