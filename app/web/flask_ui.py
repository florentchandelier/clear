# app/web/flask_ui.py
# Flask UI entrypoint for the dashboard/modern UI (uses routes_ui.py)

from flask import Flask
import os

def create_ui_app():
    """Create the main Flask UI app with all blueprints registered."""
    app = Flask(
        __name__,
        static_folder="static",          # ensure /static points to app/web/static
        template_folder="templates"      # ensure Jinja templates are loaded
    )

    # 🔒 Secret key for flash/session (override via env for production)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev_secret_key_change_me")

    # ─────────────────────────────────────────────────────────────
    # Register blueprints
    # ─────────────────────────────────────────────────────────────
    from app.web.routes_ui import ui, api
    from app.web.routes_categories import categories_bp

    # UI pages and dashboards
    app.register_blueprint(ui)

    # API endpoints (JSON responses for UI/JS)
    app.register_blueprint(api, url_prefix="/api")

    # Categories management endpoints (editing, seeding, etc.)
    app.register_blueprint(categories_bp)

    # ─────────────────────────────────────────────────────────────
    # Optional: sanity check routes
    # ─────────────────────────────────────────────────────────────
    @app.route("/ping")
    def ping():
        return {"ok": True, "msg": "CLEAR UI running"}, 200

    @app.template_filter('datetimeformat')
    def datetimeformat(value, fmt="%b %d, %Y"):
        if not value:
            return ""
        from datetime import datetime
        try:
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            return value.strftime(fmt)
        except Exception:
            return value

    @app.template_filter("money")
    def money_format(value):
        try:
            return f"{float(value):,.2f}"
        except Exception:
            return "0.00"

    return app


def run_web_ui():
    """Launch the Flask UI for local development."""
    app = create_ui_app()
    app.run(host="127.0.0.1", port=5000, debug=True)
