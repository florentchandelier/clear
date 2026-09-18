"""Release-snapshot integrity checks for licensing, branding, and links."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
POLYFORM_SHA256 = (
    "7573a6c93397a51d957542a3e258750699c3bf1c437b4b85541ec551b87066a6"
)
LICENSE_FILES = (
    "LICENSE.md",
    "LICENSE-POLYFORM-NONCOMMERCIAL-1.0.0.md",
    "LICENSING.md",
    "NOTICE.txt",
    "TRADEMARK-POLICY.md",
    "CLA.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)
PLACEHOLDERS = (
    "[" + "PROJECT NAME]",
    "[" + "LICENSOR LEGAL NAME]",
    "[" + "YEAR-YEAR]",
    "[" + "COMMERCIAL LICENSING EMAIL]",
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
PUBLIC_TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".txt"}


def tracked_files():
    """Return release files, excluding ignored/generated working-tree content."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [REPO_ROOT / path for path in result.stdout.split("\0") if path]


def test_licensing_bundle_is_complete_and_has_no_placeholders():
    for relative_path in LICENSE_FILES:
        path = REPO_ROOT / relative_path
        assert path.is_file(), relative_path
        content = path.read_text(encoding="utf-8")
        assert not any(token in content for token in PLACEHOLDERS), relative_path

    assert not (REPO_ROOT / "LICENSE").exists()


def test_polyform_license_is_the_reviewed_unmodified_text():
    content = (REPO_ROOT / "LICENSE-POLYFORM-NONCOMMERCIAL-1.0.0.md").read_bytes()
    assert hashlib.sha256(content).hexdigest() == POLYFORM_SHA256


def test_public_docs_describe_the_source_available_model_accurately():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    licensing = (REPO_ROOT / "LICENSING.md").read_text(encoding="utf-8")

    assert "source-available, not OSI-approved open source" in readme
    assert "source-available rather than OSI-approved open source" in licensing
    assert "florent@chandelier.io" in readme + licensing


def test_submission_based_cla_is_conspicuous_and_consistent():
    cla = (REPO_ROOT / "CLA.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    pull_request = (
        REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
    ).read_text(encoding="utf-8")

    assert "Version 1.1 - Submission Acceptance" in cla
    assert "By intentionally submitting a Contribution" in cla
    assert "No separate CLA signature" in contributing
    assert "By submitting this pull request" in pull_request
    assert "(CLA.md)" in contributing
    assert "(../CLA.md)" in pull_request


def test_markdown_file_links_resolve():
    missing = []
    for document in tracked_files():
        if document.suffix != ".md":
            continue
        content = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK_RE.findall(content):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(REPO_ROOT)} -> {target}")

    assert missing == []


def test_private_release_only_material_is_absent():
    excluded = (
        "agent",
        "screenshots",
        "scripts/apply_release_branding.py",
        "scripts/one_off_asset_class.py",
        "scripts/one_off_asset_class_check.py",
        "scripts/verify_transfer_impact.py",
        "tests/test_apply_release_branding.py",
    )
    assert [path for path in excluded if (REPO_ROOT / path).exists()] == []


def test_public_text_has_no_dead_agent_links_or_old_brand_name():
    old_brand = "Mi" + "nt"
    internal_docs_prefix = "agent" + "/"
    findings = []
    for path in tracked_files():
        if not path.is_file():
            continue
        if path.suffix not in PUBLIC_TEXT_SUFFIXES and path.name not in {
            ".gitignore",
            "Makefile",
        }:
            continue
        content = path.read_text(encoding="utf-8")
        if internal_docs_prefix in content or re.search(rf"\b{old_brand}\b", content):
            findings.append(path.relative_to(REPO_ROOT).as_posix())

    assert findings == []
