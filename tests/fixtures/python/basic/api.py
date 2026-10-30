"""FastAPI-style registrations."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/x")
def create_x():
    return {}


@router.get("/items/{item_id}")
def read_item(item_id: int):
    return {"item_id": item_id}


@router.api_route("/multi", methods=["GET", "POST"])
def multi():
    return {}
