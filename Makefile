# ─────────────────────────────────────────────────────────────
# Project Makefile
# ─────────────────────────────────────────────────────────────

.PHONY: help install demo run test test-importers test-fast clean

# Default target
help:
	@echo ""
	@echo "CLEAR — personal finance app"
	@echo ""
	@echo "Available targets:"
	@echo "  make install         Create venv/ and install dependencies"
	@echo "  make demo            Build the synthetic demo profile (var/demo/)"
	@echo "  make run             Run the app"
	@echo "  make test            Run all tests"
	@echo "  make test-importers  Run importer contract tests only"
	@echo "  make test-fast       Run tests without slow markers"
	@echo "  make clean           Remove caches and temporary files"
	@echo ""

# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────

# Installs runtime *and* dev dependencies, so that the documented
# `make install && make demo && make test` flow works from a fresh
# checkout (requirements.txt alone contains no test runner).
install:
	python3 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Build the deterministic synthetic demo profile at var/demo/ from the
# tracked seed under data/demo_seed/. Never touches personal/. This is
# the fast path: it ingests tracked JSON directly and needs no PDF
# parsing, so it works without Camelot's system dependency (ghostscript)
# -- see scripts/build_demo_profile.py.
demo:
	venv/bin/python scripts/build_demo_profile.py

# Execute App
run:
	venv/bin/python run.py web_ui

# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

test:
	venv/bin/pytest tests

test-importers:
	venv/bin/pytest tests/importers

test-fast:
	venv/bin/pytest tests -m "not slow"

# ─────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────

clean:
	@echo "Cleaning up..."
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
