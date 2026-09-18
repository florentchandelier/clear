import logging
from app.config import load_settings

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

def configure_logging() -> None:
    # Read-only: this runs from app/__init__.py, i.e. on *any* import of
    # the package. Creating or repairing a settings file as a side effect
    # of an import would mutate whichever profile is active -- including
    # the user's real personal profile, and including during `make demo`.
    settings = load_settings(create_missing=False)

    # Global log level
    level_name = settings.get("log_level", "INFO").upper()
    level = _LEVELS.get(level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Optional per-module overrides
    for mod in settings.get("log_modules", []):
        logging.getLogger(mod).setLevel(logging.DEBUG)

    # Silence noisy third-party libs
    #logging.getLogger("pdfplumber").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    #logging.getLogger("camelot").setLevel(logging.WARNING)
