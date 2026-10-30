"""FastAPI router mounted at /v1 by main.py."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/things")
def list_things():
    return []
