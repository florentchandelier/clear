# aggregate_mint_app.py
from __future__ import annotations

import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────

ROOT_DIR = Path("app").resolve()
OUTPUT_FILE = Path("mint_app_aggregated.txt").resolve()

# ─────────────────────────────────────────────────────────────
# Inclusion rules
# ─────────────────────────────────────────────────────────────

INCLUDE_EXTENSIONS = {
    ".py",
    #  ".md",
    #  ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".html",
    ".js",
}

# ─────────────────────────────────────────────────────────────
# Exclusion rules
# ─────────────────────────────────────────────────────────────

# Generic directories to always exclude
EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    ".env",
    ".idea",
    ".pytest_cache",
}

# Exclude some top-level app subpackages explicitly
EXCLUDE_TOP_LEVEL_DIRS = {
    "cli",  # CLI not needed for UI / app logic aggregation
}

# Optional: exclude specific files by name
EXCLUDE_FILENAMES = {
    "routes.py",
    "flask_app.py",
}

# Optional: exclude specific subpaths (relative to app/)
EXCLUDE_SUBPATH_PREFIXES = {
    # examples:
    # "services/importers/examples",
}

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def should_exclude_path(path: Path) -> bool:
    """Exclude based on directory names or explicit subpaths."""
    try:
        rel = path.relative_to(ROOT_DIR)
    except ValueError:
        return True

    # Exclude top-level folders
    if rel.parts and rel.parts[0] in EXCLUDE_TOP_LEVEL_DIRS:
        return True

    # Exclude explicit subpath prefixes
    rel_str = rel.as_posix()
    for prefix in EXCLUDE_SUBPATH_PREFIXES:
        if rel_str.startswith(prefix):
            return True

    return False


def should_include_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in INCLUDE_EXTENSIONS
        and path.name not in EXCLUDE_FILENAMES
        and not any(part in EXCLUDE_DIR_NAMES for part in path.parts)
        and not should_exclude_path(path)
    )


# ─────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────

def aggregate() -> None:
    files = sorted(
        p for p in ROOT_DIR.rglob("*") if should_include_file(p)
    )

    with OUTPUT_FILE.open("w", encoding="utf-8") as out:
        out.write("# ============================================================\n")
        out.write("# Aggregated CLEAR (backend + UI)\n")
        out.write("# ============================================================\n\n")
        out.write(f"# Root: {ROOT_DIR}\n")
        out.write(f"# Files aggregated: {len(files)}\n\n")

        for file_path in files:
            rel_path = file_path.relative_to(ROOT_DIR)

            out.write("\n")
            out.write("# ============================================================\n")
            out.write(f"# FILE: app/{rel_path.as_posix()}\n")
            out.write("# ============================================================\n\n")

            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                out.write("# [Skipped: could not decode file]\n")
                out.write("# ------------------------- END FILE -------------------------\n")
                continue

            out.write(content)
            if not content.endswith("\n"):
                out.write("\n")

            out.write("\n")
            out.write("# ------------------------- END FILE -------------------------\n")

    print(f"✅ Aggregated {len(files)} files into {OUTPUT_FILE}")


if __name__ == "__main__":
    aggregate()
