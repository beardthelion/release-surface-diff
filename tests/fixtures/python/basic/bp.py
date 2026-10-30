"""Flask blueprint mounted at /api by main.py."""

from flask import Blueprint

bp = Blueprint("b", __name__)


@bp.get("/status")
def status():
    return "ok"
