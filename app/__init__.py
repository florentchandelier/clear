# app/__init__.py
from app.logging_config import configure_logging
configure_logging()

# Package marker for the application.

__all__ = [
    "config",
    "services",
    "web",
    "cli",
]
