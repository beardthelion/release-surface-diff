"""Flask-style registrations."""

from flask import Flask

app = Flask(__name__)


@app.get("/users/<id>")
def get_user(id):
    return {"id": id}


@app.route("/health")
def health():
    return "ok"


def legacy(request):
    return "legacy"


def plain(request):
    return "plain"


app.add_url_rule("/legacy", view_func=legacy, methods=["GET"])
app.add_url_rule("/plain", view_func=plain)
