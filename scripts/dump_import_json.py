#!/usr/bin/env python3
"""Auto-detect and convert PDF statements into JSON exports.

Runs every registered importer's detect()/parse_to_json() against a
directory of real PDF statements, validates the output against the
shared importer schema, and writes one JSON file per input PDF.

This replaces a version that hardcoded the author's own personal
statement directory and only knew about 4 of the 8 registered importers.
It now takes its input/output directories as CLI arguments and discovers
importers through the registry (app/services/importers/registry.py),
so adding a new importer there makes it available here automatically.

Usage:
  venv/bin/python scripts/dump_import_json.py --input personal/statements
  venv/bin/python scripts/dump_import_json.py --input personal/statements --output personal/statements/json_exports --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import ACCOUNT_CLASSES_BY_SIDE, ACCOUNT_SIDES  # noqa: E402
from app.services.importers.registry import list_importers  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "app" / "services" / "importers" / "schema.json"

# The suggested (but not required -- nothing in the app reads it
# automatically) home for real statements a user wants to keep archived;
# see personal/README.md.
DEFAULT_INPUT_DIR = REPO_ROOT / "personal" / "statements"


def all_importers():
    """Every importer registered for any (account_side, account_class)."""
    for side in ACCOUNT_SIDES:
        for cls in ACCOUNT_CLASSES_BY_SIDE.get(side, []):
            yield from list_importers(side, cls)


def load_validator(schema_path: Path) -> jsonschema.Draft7Validator:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def detect_importer(pdf_path: Path, importers):
    """Return the first importer whose detect() matches this PDF."""
    for importer in importers:
        try:
            if importer.detect(pdf_path):
                return importer
        except Exception as e:
            print(f"⚠️  detect() failed for {pdf_path.name} via {importer.key}: {e}")
    return None


def validate_against_schema(data: dict, name: str, validator: jsonschema.Draft7Validator) -> bool:
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if not errors:
        print(f"   ✅ schema validation passed for {name}")
        return True
    print(f"   ❌ schema validation failed for {name}:")
    for err in errors[:5]:
        path = " → ".join(map(str, err.path)) or "(root)"
        print(f"      - {path}: {err.message}")
    return False


def process_pdf(pdf_path: Path, out_dir: Path, importers, validator, *, force: bool) -> bool:
    out_path = out_dir / f"{pdf_path.stem}.json"
    if out_path.exists() and not force:
        print(f"⏩ skipping {pdf_path.name} (already exported; use --force to overwrite)")
        return True

    importer = detect_importer(pdf_path, importers)
    if importer is None:
        print(f"❌ no matching importer for: {pdf_path.name}")
        return False

    print(f"📄 {importer.label} → {pdf_path.name}")
    try:
        data = importer.parse_to_json(pdf_path)
    except Exception as e:
        print(f"❌ error parsing {pdf_path.name} with {importer.key}: {e}")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ exported → {out_path}")
    return validate_against_schema(data, pdf_path.name, validator)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT_DIR,
        help=f"directory of PDF statements to convert (default: {DEFAULT_INPUT_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="directory to write JSON exports to (default: <input>/json_exports)",
    )
    parser.add_argument("--force", action="store_true", help="re-export files that already have a JSON export")
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH, help="path to the importer JSON schema")
    args = parser.parse_args(argv)

    input_dir: Path = args.input
    output_dir: Path = args.output or (input_dir / "json_exports")

    if not input_dir.is_dir():
        print(f"📂 input directory does not exist: {input_dir}")
        return 1

    validator = load_validator(args.schema)
    importers = list(all_importers())
    print(f"📘 loaded schema from {args.schema}; {len(importers)} importer(s) registered")

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"📂 no PDF files found in {input_dir}")
        return 0

    print(f"🔍 found {len(pdf_files)} PDF(s) in {input_dir}")
    ok = True
    for pdf_path in pdf_files:
        ok = process_pdf(pdf_path, output_dir, importers, validator, force=args.force) and ok

    print(f"\n🎉 done — JSON files in: {output_dir}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
