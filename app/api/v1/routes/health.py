"""Health check routes for the Ollama Proxy Server API."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthStatus(BaseModel):
    """Health status response model."""

    status: str


@router.get("/health", response_model=HealthStatus)
async def health_check():
    """Endpoint to verify that the server is running."""
    return {"status": "ok"}
