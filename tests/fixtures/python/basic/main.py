"""Cross-file mounts: blueprint and include_router prefixes."""

from flask import Flask

from bp import bp
from items import router as items_router

app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/api")
app.include_router(items_router, prefix="/v1")
