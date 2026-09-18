"""Tests for scripts/audit_public_tree.py (Milestone 1 of the CLEAR
source-available release plan).

These exercise the pure rule functions directly (no git subprocess, no
real repository state) so they run fast and deterministically anywhere.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import audit_public_tree as audit_mod  # noqa: E402
from scripts.audit_public_tree import (  # noqa: E402
    audit_file_contents,
    audit_history_contents,
    audit_refs,
    audit_tracked_paths,
    default_private_fragments,
    is_forbidden,
    scan_json_for_unmasked_fields,
    scan_text_for_fragments,
)
from scripts.audit_public_tree import _hash_value  # noqa: E402


def test_forbidden_paths_are_flagged():
    assert is_forbidden("settings.json") is not None
    assert is_forbidden("categories_custom.json") is not None
    assert is_forbidden("categories_custom.json.backup") is not None
    assert is_forbidden("debug_parquet_statements.zip") is not None
    assert is_forbidden("debug_parquet_statements/foo.parquet") is not None
    assert is_forbidden("parquet_statements/2025/foo.parquet") is not None
    assert is_forbidden("pdf_statements/2026/bmo/statement.pdf") is not None
    assert is_forbidden("personal/settings.json") is not None
    assert is_forbidden("var/demo/settings.json") is not None
    assert is_forbidden("mint_app_aggregated.txt") is not None


def test_allowlist_overrides_forbidden_patterns():
    assert is_forbidden("personal/README.md") is None
    assert is_forbidden("personal/settings.example.json") is None
    assert is_forbidden("tests/importers/fixtures/pdf/bmo_chequing_demo.pdf") is None


def test_safe_paths_are_not_flagged():
    assert is_forbidden("app/services/queries.py") is None
    assert is_forbidden("categories.json") is None
    assert is_forbidden("tests/importers/fixtures/bmo_mastercard_fr.json") is None
    assert is_forbidden("data/demo_seed/mock_bmo_chequing.json") is None


def test_audit_tracked_paths_reports_rule_and_forbidden_path_only():
    findings = audit_tracked_paths(["settings.json", "app/config.py"])
    assert len(findings) == 1
    assert findings[0].rule == "FORBIDDEN_PATH"
    assert findings[0].path == "settings.json"


def test_fragment_scan_matches_path_like_fragment_as_substring():
    fragments = ["/home/alice/project"]
    assert scan_text_for_fragments("path = '/home/alice/project/data'", fragments)
    assert not scan_text_for_fragments("path = '/opt/app/data'", fragments)


def test_fragment_scan_uses_word_boundaries_for_bare_username():
    # Regression: a short username like "devuser" must not match as a raw
    # substring of ordinary words such as "cash flow" / "workflow" in a
    # finance app -- only standalone occurrences should count.
    fragments = ["devuser"]
    assert not scan_text_for_fragments("def income_vs_expense_flow(): ...", fragments)
    assert not scan_text_for_fragments("workflow_state = 'pending'", fragments)
    assert scan_text_for_fragments(
        "INPUT_DIR = '/home/devuser/pdf_statements'", fragments,
    )
    assert scan_text_for_fragments(
        "author: devuser <devuser@example.com>", fragments,
    )


def test_unmasked_json_field_is_flagged_with_its_location_and_value():
    data = {
        "cardholder_info": {
            "name": "MME XXX YYY",
            "card_number": "4111111111111111",
        }
    }
    findings = scan_json_for_unmasked_fields(data)
    assert findings == [("$.cardholder_info.card_number", "4111111111111111")]
    # The CLI-facing Finding built from this never carries the value (see
    # audit_file_contents / Finding.__str__) -- the raw value is returned
    # here only so a caller can hash it against a reviewed exception.


def test_masked_card_number_is_not_flagged():
    data = {"cardholder_info": {"card_number": "XXXX XXXX XXXX 1234"}}
    assert scan_json_for_unmasked_fields(data) == []


def test_repeated_digit_placeholder_account_number_is_not_flagged():
    # Matches this repo's own existing, already-reviewed fixture value.
    data = {"account": {"account_number": "1111111111"}}
    assert scan_json_for_unmasked_fields(data) == []


def test_placeholder_email_is_not_flagged():
    data = {"contact": {"email": "example@example.com"}}
    assert scan_json_for_unmasked_fields(data) == []


def test_real_looking_email_is_flagged():
    data = {"contact": {"email": "florent.smith@gmail.com"}}
    assert scan_json_for_unmasked_fields(data) == [
        ("$.contact.email", "florent.smith@gmail.com")
    ]


def test_card_digits_field_is_not_treated_as_suspicious():
    # card_digits (last-4 only) is a designed-safe truncated field, distinct
    # from the full card_number -- it should never trigger a finding on its
    # own, matching this repo's own account fixtures.
    data = {"cardholder_info": {"card_digits": "1234"}}
    assert scan_json_for_unmasked_fields(data) == []


def test_audit_refs_flags_only_non_standard_refs():
    refs = [
        "refs/heads/main",
        "refs/remotes/origin/main",
        "refs/tags/v1",
        "refs/codex/turn-diffs/checkpoints/abc",
    ]
    findings = audit_refs(refs)
    assert len(findings) == 1
    assert findings[0].rule == "NON_STANDARD_REF"
    assert findings[0].path == "refs/codex/turn-diffs/checkpoints/abc"


def test_default_private_fragments_include_repo_root(tmp_path):
    fragments = default_private_fragments(tmp_path)
    assert str(tmp_path.resolve()) in fragments


def test_audit_file_contents_flags_fragment_in_tracked_file(tmp_path):
    repo = tmp_path
    (repo / "scripts").mkdir()
    bad = repo / "scripts" / "dump.py"
    bad.write_text(f"INPUT_DIR = '{repo}/pdf_statements'\n")
    findings = audit_file_contents(repo, ["scripts/dump.py"], [str(repo)])
    assert any(f.rule == "PRIVATE_PATH_FRAGMENT" for f in findings)


def _write_json(root: Path, rel: str, payload: dict) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return rel


REVIEWED_PATH = "data/demo_seed/mock_bmo_chequing.json"
REVIEWED_FIELD_PAYLOAD = {
    "statements": [{"cardholder_info": {"card_number": "12345678-001"}}]
}


def test_reviewed_exception_waives_only_that_exact_file_and_field(tmp_path):
    rel = _write_json(tmp_path, REVIEWED_PATH, REVIEWED_FIELD_PAYLOAD)
    waived = []

    findings = audit_file_contents(tmp_path, [rel], [], waived=waived)

    assert findings == [], "the reviewed exception should suppress this finding"
    assert len(waived) == 1
    assert waived[0].rule == "UNMASKED_JSON_FIELD"
    assert waived[0].path == REVIEWED_PATH
    assert waived[0].detail == "$.statements[0].cardholder_info.card_number"
    assert waived[0].reason, "a waiver must carry its reviewed reason"


def test_reviewed_exception_does_not_waive_the_same_field_in_another_file(tmp_path):
    rel = _write_json(tmp_path, "data/demo_seed/other_seed.json", REVIEWED_FIELD_PAYLOAD)
    waived = []

    findings = audit_file_contents(tmp_path, [rel], [], waived=waived)

    assert waived == []
    assert [f.rule for f in findings] == ["UNMASKED_JSON_FIELD"]


def test_reviewed_exception_does_not_waive_another_field_in_the_same_file(tmp_path):
    rel = _write_json(
        tmp_path,
        REVIEWED_PATH,
        {"account_summary": {"account_number": "98765432-9"}},
    )
    waived = []

    findings = audit_file_contents(tmp_path, [rel], [], waived=waived)

    assert waived == []
    assert [f.rule for f in findings] == ["UNMASKED_JSON_FIELD"]
    assert findings[0].detail == "$.account_summary.account_number"


def test_reviewed_exception_is_reported_not_silently_dropped(tmp_path):
    rel = _write_json(tmp_path, REVIEWED_PATH, REVIEWED_FIELD_PAYLOAD)
    waived = []
    audit_file_contents(tmp_path, [rel], [], waived=waived)
    rendered = str(waived[0])
    assert rendered.startswith("[waived: UNMASKED_JSON_FIELD]")
    assert REVIEWED_PATH in rendered
    # ...and still never echoes the value itself.
    assert "12345678-001" not in rendered


def test_the_real_tracked_seed_is_covered_by_the_reviewed_exception():
    """The waiver must match the file as it actually is on disk -- if the
    seed's value or shape changes, the exception should stop applying and
    the finding should come back for review."""
    key = (REVIEWED_PATH, "$.statements[0].cardholder_info.card_number")
    assert key in audit_mod.REVIEWED_JSON_FIELD_EXCEPTIONS
    expected_hash, reason = audit_mod.REVIEWED_JSON_FIELD_EXCEPTIONS[key]
    assert reason, "a waiver must carry its reviewed reason"

    repo_root = Path(__file__).resolve().parents[1]
    waived = []
    findings = audit_file_contents(repo_root, [REVIEWED_PATH], [], waived=waived)
    assert findings == []
    assert len(waived) == 1


def test_exception_is_bound_to_the_reviewed_value_not_just_its_location(tmp_path):
    """The exact failure mode this exists to close: swapping a *different*
    realistic-looking unmasked value into the reviewed (path, field)
    location must NOT be silently waived. A location-only exception (the
    first implementation of D14) would let a real account number pass
    the release gate simply by landing at the same spot."""
    swapped_in_value = "4111111111111111"
    assert _hash_value(swapped_in_value) != audit_mod.REVIEWED_JSON_FIELD_EXCEPTIONS[
        (REVIEWED_PATH, "$.statements[0].cardholder_info.card_number")
    ][0], "test fixture error: the swapped-in value must not coincide with the reviewed one"

    rel = _write_json(
        tmp_path,
        REVIEWED_PATH,
        {"statements": [{"cardholder_info": {"card_number": swapped_in_value}}]},
    )
    waived = []

    findings = audit_file_contents(tmp_path, [rel], [], waived=waived)

    assert waived == [], "a changed value at a reviewed location must not be waived"
    assert [f.rule for f in findings] == ["UNMASKED_JSON_FIELD"]
    assert findings[0].path == rel
    assert findings[0].detail == "$.statements[0].cardholder_info.card_number"


def test_reviewed_value_hash_matches_the_documented_derivation():
    """The stored hash must actually be sha256 of the value in the comment
    above it, so the exception can't drift from what a reviewer thinks
    they approved."""
    key = (REVIEWED_PATH, "$.statements[0].cardholder_info.card_number")
    expected_hash, _reason = audit_mod.REVIEWED_JSON_FIELD_EXCEPTIONS[key]
    assert expected_hash == _hash_value("12345678-001")


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


@pytest.fixture
def repo_with_scrubbed_secret(tmp_path):
    """A repo where a private path was committed inside an innocuous file
    and then removed in a later commit -- the blob remains reachable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")

    notes = repo / "notes.txt"
    notes.write_text("scratch notes\nINPUT_DIR = /home/someone/private_archive\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "add notes")

    notes.write_text("scratch notes\n")  # sanitised, but history keeps the blob
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "scrub notes")
    return repo


def test_history_content_scan_finds_a_removed_private_fragment(repo_with_scrubbed_secret):
    fragment = "/home/someone/private_archive"
    repo = repo_with_scrubbed_secret

    # The current worktree is clean, so a tracked-only scan sees nothing...
    tracked = audit_file_contents(repo, ["notes.txt"], [fragment])
    assert tracked == []

    # ...but the blob is still in history and must be reported.
    historical = audit_history_contents(repo, [fragment])
    assert historical, "a scrubbed-but-reachable private fragment was missed"
    assert {f.rule for f in historical} == {"HISTORICAL_PRIVATE_FRAGMENT"}
    assert historical[0].path == "notes.txt"
    # The finding names the location, never the matched value.
    assert fragment not in str(historical[0])


def test_history_content_scan_is_quiet_on_a_clean_repo(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('hello')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "init")

    assert audit_history_contents(repo, ["/home/someone/private_archive"]) == []


def test_audit_file_contents_ignores_binary_extensions(tmp_path):
    repo = tmp_path
    pdf = repo / "statement.pdf"
    pdf.write_bytes(str(repo).encode())
    findings = audit_file_contents(repo, ["statement.pdf"], [str(repo)])
    assert findings == []
