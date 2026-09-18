# app/config.py

import json
import os
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).parent.resolve()

# -------- Model: sides and classes --------
ACCOUNT_SIDES: List[str] = ["asset", "liability"]

ACCOUNT_CLASSES_BY_SIDE: Dict[str, List[str]] = {
    "asset":     ["cash", "investment", "home_fmv", "car_fmv", "private_equity", "insurance_fv", "other_fmv"],
    "liability": ["loan", "loc", "credit_card", "other_debt"],
}

# -------- Asset classification metadata --------
# asset_nature: real | financial | intangible
# liquidity_class: liquid | illiquid

ASSET_CLASS_METADATA = {
    # ──────────────────────────────────────────
    # Financial — liquid
    # ──────────────────────────────────────────
    "cash": {
        "asset_nature": "financial",
        "liquidity_class": "liquid",
    },
    "investment": {  # publicly traded portfolio
        "asset_nature": "financial",
        "liquidity_class": "liquid",
    },

    # ──────────────────────────────────────────
    # Financial — illiquid
    # ──────────────────────────────────────────
    "private_equity": {
        "asset_nature": "financial",
        "liquidity_class": "illiquid",
    },

    # ──────────────────────────────────────────
    # Real — illiquid
    # ──────────────────────────────────────────
    "home_fmv": {
        "asset_nature": "real",
        "liquidity_class": "illiquid",
    },
    "car_fmv": {
        "asset_nature": "real",
        "liquidity_class": "illiquid",
    },

    # ──────────────────────────────────────────
    # Intangible — illiquid
    # ──────────────────────────────────────────
    "insurance_fv": {
        "asset_nature": "intangible",
        "liquidity_class": "illiquid",
    },
    "other_fmv": {  # keep as fallback / legacy
        "asset_nature": "intangible",
        "liquidity_class": "illiquid",
    },
}

ACCOUNT_CLASS_LABELS = {
    "cash": "Cash Account",
    "investment": "Investment",
    "credit_card": "Credit Card",
    "loan": "Loan",
    "loc": "Line of Credit",
    "other_debt": "Other Debt",
    "home_fmv": "Home FMV",
    "car_fmv": "Car FMV",
    "other_fmv": "Other FMV",
    "private_equity": "Private Equity",
    "insurance_fv": "Insurance"
}

# Classes that use manual time-series values (entered in UI)
FMV_CLASSES: List[str] = ["home_fmv", "car_fmv", "other_fmv"]
MANUAL_BALANCE_CLASSES: List[str] = ["other_debt"]

# -------- Settings / profile resolution --------
#
# Three "profiles" can supply settings.json and the paths it points at:
#   1. MINT_PROFILE_DIR (env var) -- an explicit external profile, e.g. for
#      CI/containers. Highest precedence.
#   2. personal/ -- a real-data profile. Selected only when
#      personal/settings.json already exists; load_settings() never
#      creates it -- activating personal mode is an explicit action (see
#      personal/README.md), never an implicit side effect.
#   3. var/demo/ -- the synthetic demo profile. Default when neither of the
#      above applies; its settings.json is created lazily on first load.
#
# categories.json (the base template) is intentionally NOT profile-scoped:
# it stays tracked and read-only, shared by every profile.
#
# Profile isolation is validated by tests/test_config_profiles.py.

REPO_ROOT = HERE.parent
PERSONAL_DIR = REPO_ROOT / "personal"
DEMO_DIR = REPO_ROOT / "var" / "demo"
BASE_CATEGORIES_PATH = (REPO_ROOT / "categories.json").resolve()


def active_profile_dir() -> Path:
    """Resolve the active profile directory. Has no side effects -- it only
    checks whether personal/settings.json exists, never creates anything.

    Precedence: MINT_PROFILE_DIR env var > existing personal/settings.json
    > var/demo/.
    """
    override = os.environ.get("MINT_PROFILE_DIR", "").strip()
    if override:
        return Path(override).resolve()
    if (PERSONAL_DIR / "settings.json").exists():
        return PERSONAL_DIR.resolve()
    return DEMO_DIR.resolve()


def active_settings_path() -> Path:
    """Path to settings.json inside the currently active profile."""
    return active_profile_dir() / "settings.json"


def active_profile_label() -> str:
    """Human-readable label for the active profile, for display in the UI:
    'external' (MINT_PROFILE_DIR), 'personal', or 'demo'. Mirrors the same
    precedence as active_profile_dir()."""
    if os.environ.get("MINT_PROFILE_DIR", "").strip():
        return "external"
    if (PERSONAL_DIR / "settings.json").exists():
        return "personal"
    return "demo"


def _resolve_relative(value: str, base: Path) -> str:
    """Resolve a possibly-relative path string against `base`.

    Absolute inputs pass through (normalized). Falsy input is returned
    unchanged so a blank settings field doesn't get turned into `base`.
    """
    if not value:
        return value
    p = Path(value)
    if not p.is_absolute():
        p = base / p
    return str(p.resolve())


# Profile-independent defaults (fuzzy/clustering, logging, FX). The
# path-shaped keys are filled in per-profile by
# _default_settings_for_profile() below.
_STATIC_DEFAULTS: Dict = {
    "similarity_threshold": 80,
    "min_cluster_tx": 1,
    "single_tx_bucket": "OTHER_SMALL_CLUSTER",
    "overwrite_duplicates": False,

    "debug": False,                 # global debug switch
    "log_level": "INFO",            # INFO | DEBUG | WARNING | ERROR
    "log_modules": [],              # optional per-module overrides

    "display_currency": "CAD",
    "fx_supported_currencies": ["CAD", "USD", "EUR"],
}

# These locations are part of the generated demo profile's structure, not
# user configuration. Persisting their fully resolved values freezes the
# current physical content-directory name and can make a later demo promotion
# point back at obsolete content. Personal and explicit external profiles
# remain free to configure the same keys.
_DEMO_MANAGED_PATH_KEYS = frozenset({
    "consolidated_statements",
    "custom_categories_path",
})


def _default_settings_for_profile(profile_dir: Path) -> Dict:
    """Default settings for `profile_dir`, as if its settings.json didn't
    exist yet -- i.e. what a fresh profile starts from."""
    data = dict(_STATIC_DEFAULTS)
    data["consolidated_statements"] = str((profile_dir / "consolidated_statements").resolve())
    data["base_categories_path"] = str(BASE_CATEGORIES_PATH)
    data["custom_categories_path"] = str((profile_dir / "categories_custom.json").resolve())
    # Deliberately NOT os.environ.get("MINT_DATA_DIR"): baking the env var
    # into a profile's defaults persists it to that profile's settings.json
    # on first run, after which the override outlives the environment
    # variable that set it. MINT_DATA_DIR is read from the environment at
    # load time instead (see load_settings). A user may still set
    # data_dir_env explicitly in their own settings file, and that is
    # honoured -- it just is never written there on their behalf.
    data["data_dir_env"] = ""
    return data


def _normalize_fx_settings(settings: Dict) -> None:
    # Normalize display currency
    display = settings.get("display_currency", "CAD")
    if isinstance(display, str):
        display = display.upper().strip()
    else:
        display = "CAD"

    settings["display_currency"] = display

    # Normalize supported currencies
    fx_ccy = settings.get("fx_supported_currencies", [])
    if not isinstance(fx_ccy, list):
        fx_ccy = []

    fx_ccy = [
        c.upper().strip()
        for c in fx_ccy
        if isinstance(c, str) and len(c.strip()) == 3
    ]

    # Ensure display currency is included
    if display not in fx_ccy:
        fx_ccy.append(display)

    settings["fx_supported_currencies"] = sorted(set(fx_ccy))


def load_settings(*, create_missing: bool = True) -> Dict:
    """Load settings for the currently active profile, merged with that
    profile's defaults.

    Pass create_missing=False for a pure read: the returned values are the
    same, but a missing or corrupt settings file is left exactly as it is
    rather than being created or repaired. Callers that merely want to
    *inspect* settings (logging setup, diagnostics) must use that mode --
    otherwise simply importing them mutates whichever profile happens to
    be active, including the user's real personal one.

    Behavior:
    - Resolves the active profile (see active_profile_dir()).
    - Starts from that profile's defaults
      (_default_settings_for_profile()).
    - If that profile's settings.json exists, merges its contents.
    - If missing, creates it from defaults -- this only ever creates a
      demo-profile (or explicit MINT_PROFILE_DIR) settings file; a missing
      personal/settings.json is never auto-created, since
      active_profile_dir() only selects the personal profile once that
      file already exists.
    - If present but corrupted, repairs it in place with that profile's
      defaults (never writes to a different profile).
    - For the generated demo profile, ignores persisted
      consolidated_statements/custom_categories_path values, including
      leftovers written by older versions. Those paths are always recomputed
      from the current demo directory. Personal and explicit external
      profiles still honour both settings.
    - Relative paths found in the file are normalized to absolute, anchored
      at the profile dir (base_categories_path is anchored at the
      repository root instead, since the base template is not
      profile-scoped).
    - If MINT_DATA_DIR env var is set, overrides consolidated_statements
      only, regardless of which profile is active and regardless of
      whether this is a first-run creation or a normal load. The override
      is applied in-memory only and is never written into the persisted
      settings.json, so removing the env var later doesn't leave a stale
      path baked into the profile.
    - Does NOT auto-save on every call beyond first-creation/repair
      (prevents overwriting user edits made outside this process).
    - An auto-created or auto-repaired settings.json never persists
      consolidated_statements/custom_categories_path -- those are always
      recomputed fresh against the profile directory on every call, so an
      auto-created file can never freeze a path that later goes stale
      relative to it (BUG-03 regression; a
      profile directory can be renamed after its settings.json is first
      written, e.g. during scripts/build_demo_profile.py's atomic
      promotion). Explicit saves apply the same omission when the active
      profile is demo; personal and external profiles remain configurable.
    """
    profile_dir = active_profile_dir()
    profile_label = active_profile_label()
    settings_path = profile_dir / "settings.json"
    defaults = _default_settings_for_profile(profile_dir)

    needs_write = False
    if not settings_path.exists():
        data = dict(defaults)
        needs_write = True
    else:
        try:
            file_data = json.loads(settings_path.read_text())
        except Exception as e:
            print(f"[SETTINGS] Warning: failed to load {settings_path}: {e}")
            file_data = None
            needs_write = True
        data = dict(defaults)
        if isinstance(file_data, dict):
            if profile_label == "demo":
                file_data = {
                    key: value
                    for key, value in file_data.items()
                    if key not in _DEMO_MANAGED_PATH_KEYS
                }
            data.update(file_data)

    if data.get("consolidated_statements"):
        data["consolidated_statements"] = _resolve_relative(
            data["consolidated_statements"], profile_dir
        )
    if data.get("custom_categories_path"):
        data["custom_categories_path"] = _resolve_relative(
            data["custom_categories_path"], profile_dir
        )
    if data.get("base_categories_path"):
        data["base_categories_path"] = _resolve_relative(
            data["base_categories_path"], REPO_ROOT
        )

    _normalize_fx_settings(data)

    if needs_write and create_missing:
        # Persist everything except the two path keys that are computed
        # from -- and can therefore go stale relative to -- the profile
        # directory's current location (base_categories_path is exempt:
        # it's profile-independent, so it can't go stale this way).
        # Omitting them means every future load recomputes them fresh
        # against wherever the profile directory actually is, rather than
        # freezing a value that a later rename could invalidate (BUG-03).
        # `data` itself, returned below, is unaffected -- it already has
        # the resolved values for this call's caller to use immediately.
        persisted = {
            k: v for k, v in data.items()
            if k not in _DEMO_MANAGED_PATH_KEYS
        }
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(persisted, indent=2))
        print(f"[SETTINGS] Created new settings file at {settings_path}")
    elif not needs_write:
        print(f"[SETTINGS] Loaded settings from {settings_path}")

    # Optional override via environment variable -- data path only, applied
    # after persisting so it never gets baked into the settings file itself.
    env_dir = os.environ.get("MINT_DATA_DIR", "") or data.get("data_dir_env") or ""
    if env_dir:
        data = dict(data)
        data["consolidated_statements"] = str(Path(env_dir).resolve())

    return data


def save_settings(settings: Dict) -> None:
    """Persist settings to the active profile's settings.json.

    Uses the same profile resolution as load_settings(), so a save always
    lands wherever the next load would read from -- there is no way to
    load from one profile and unknowingly save to another.

    The generated demo profile never persists its two profile-derived path
    keys. They are recomputed on every load so an atomic demo promotion cannot
    leave settings pointing at an obsolete physical content directory.
    Personal and explicit external profiles preserve caller-supplied paths.
    """
    _normalize_fx_settings(settings)
    persisted = dict(settings)
    if active_profile_label() == "demo":
        for key in _DEMO_MANAGED_PATH_KEYS:
            persisted.pop(key, None)
    path = active_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(persisted, indent=2))
