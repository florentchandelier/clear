# app/services/normalize_parquet.py
from __future__ import annotations
from pathlib import Path
from typing import Tuple, Dict, Any
import hashlib
import pandas as pd
import re
from datetime import datetime
from datetime import date as _date
from typing import Optional

from app.services import meta_balances as mb
from app.config import ASSET_CLASS_METADATA
from app.services.utils import statement_anchor_date

import logging

LOG = logging.getLogger(__name__)

DEFAULT_KEY = "__default__"
CURRENCIES = ("EUR", "USD", "CAD")

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):
            h.update(chunk)
    return h.hexdigest()

def _stable16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]

class TransactionReconciliationError(ValueError):
    """Raised before persistence when extracted rows and identities diverge."""

    def __init__(
        self,
        *,
        extracted: int,
        normalized: int,
        unique_ids: int,
        missing_ids: int,
    ) -> None:
        self.counts = {
            "extracted": extracted,
            "normalized": normalized,
            "unique_ids": unique_ids,
            "missing_ids": missing_ids,
        }
        super().__init__(
            "transaction reconciliation failed before write: "
            f"extracted={extracted}, normalized={normalized}, "
            f"unique_ids={unique_ids}, missing_ids={missing_ids}"
        )


def _reconcile_transaction_batch(
    importer_json: Dict[str, Any],
    transactions_df: pd.DataFrame,
) -> None:
    """Require a one-to-one mapping from extracted rows to stable IDs."""
    extracted = sum(
        len(statement.get("transactions", []) or [])
        for statement in (importer_json.get("statements", []) or [])
    )
    normalized = len(transactions_df)

    if "transaction_id" in transactions_df.columns:
        ids = transactions_df["transaction_id"]
        present = ids.notna() & ids.astype(str).str.strip().ne("")
        missing_ids = int((~present).sum())
        unique_ids = int(ids[present].nunique())
    else:
        missing_ids = normalized
        unique_ids = 0

    if (
        extracted != normalized
        or normalized != unique_ids
        or missing_ids
    ):
        raise TransactionReconciliationError(
            extracted=extracted,
            normalized=normalized,
            unique_ids=unique_ids,
            missing_ids=missing_ids,
        )


def _require_unique_transaction_ids(df: pd.DataFrame, *, context: str) -> None:
    """Reject a batch whose transaction identities are not one-to-one."""
    if df.empty:
        return
    if "transaction_id" not in df.columns:
        raise ValueError(f"{context} is missing transaction_id")

    duplicate_rows = int(df["transaction_id"].duplicated(keep=False).sum())
    if duplicate_rows:
        raise ValueError(
            f"{context} contains {duplicate_rows} rows with duplicate transaction_id values"
        )

def _parse_currency(desc: str) -> str:
    if not desc:
        return "CAD"
    for cur in CURRENCIES:
        if desc.startswith(cur):
            return cur
    return "CAD"

def _meta_path(base: Path, name: str) -> Path:
    _ensure_dir(base / "_meta")
    return (base / "_meta" / name).resolve()

def _load_meta(base: Path) -> pd.DataFrame:
    mp = _meta_path(base, "statement_sources.parquet")
    if mp.exists():
        return pd.read_parquet(mp)
    return pd.DataFrame(columns=[
        "source_statement_id","file_sha256","document_type","account_date",
        "institution","account_side","account_class","account_number","account_id",
        "export_date","path"
    ])

def _upsert_meta(base: Path, new_rows: pd.DataFrame) -> None:
    mp = _meta_path(base, "statement_sources.parquet")
    if mp.exists():
        old = pd.read_parquet(mp)
        all_df = pd.concat([old, new_rows], ignore_index=True)
        all_df = all_df.drop_duplicates(subset=["source_statement_id"], keep="last")
    else:
        all_df = new_rows.drop_duplicates(subset=["source_statement_id"], keep="last")
    all_df.to_parquet(mp, index=False)


def _load_accounts(base: Path) -> pd.DataFrame:
    ap = _meta_path(base, "accounts.parquet")
    if ap.exists():
        return pd.read_parquet(ap)
    return pd.DataFrame(columns=[
        "account_id", "account_number", "name", "institution",
        "account_side", "account_class",
        "base_currency",  # ← NEW
        "asset_nature", "liquidity_class",
        "status"
    ])


def _upsert_accounts(base: Path, account_rows: pd.DataFrame) -> None:
    ap = _meta_path(base, "accounts.parquet")
    if ap.exists():
        old = pd.read_parquet(ap)
        merged = pd.concat([old, account_rows], ignore_index=True)
        merged = merged.drop_duplicates(subset=["account_id"], keep="last")
    else:
        merged = account_rows.drop_duplicates(subset=["account_id"], keep="last")
    merged.to_parquet(ap, index=False)

def _f(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def normalize_desc(desc: str) -> str:
    """Normalize description for fuzzy matching and corrections lookup."""
    desc = (desc or "").lower()
    desc = re.sub(r"[0-9]", "", desc)
    desc = re.sub(r"[^\w\s]", "", desc)
    desc = re.sub(r"\s+", " ", desc)
    return desc.strip()

def _asset_meta(account_side: str, account_class: str) -> dict:
    if account_side != "asset":
        return {}

    if account_class not in ASSET_CLASS_METADATA:
        LOG.warning(f"Unknown asset class: {account_class}")

    return ASSET_CLASS_METADATA.get(account_class, {})


def _extract_valuation_date(importer_json: dict) -> Optional[_date]:
    """
    Determine the valuation date for FX prefetch.

    Priority:
    1) First NAV snapshot date
    2) account_summary.current_balance.date
    3) None (caller decides fallback)
    """
    # NAV-first (investment accounts)
    navs = importer_json.get("nav_snapshots") or []
    if navs:
        d = navs[0].get("date")
        if d:
            return pd.to_datetime(d, errors="coerce").date()

    # Fallbacks for transactional accounts
    acc = importer_json.get("account_summary") or {}
    curr = acc.get("current_balance")
    if isinstance(curr, dict) and curr.get("date"):
        return pd.to_datetime(curr["date"], errors="coerce").date()

    return None

# ──────────────────────────────────────────────────────────────
# Main ingestion helpers
# ──────────────────────────────────────────────────────────────
def normalize_importer_json(
    importer_json: Dict[str, Any],
    institution: str,
    account_side: str,
    account_class: str,
    source_pdf: Path,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Normalize importer JSON into transaction, meta, account, and balance DataFrames.

    Option A implemented:
        • Supports statement-level parent_account_id (new)
        • Supports cardholder_info for credit cards (legacy)
        • Supports top-level account_summary.account_id
        • Prevents synthetic phantom accounts
    """

    export_date = importer_json.get("export_date")
    doc_sig = importer_json.get("document_signature", {}) or {}
    document_type = doc_sig.get("document_type") or importer_json.get("document_type") or "UNKNOWN"

    # Extract statement date if present (credit cards use previous_balance)
    # ordered by priority
    date_fields = ["current_balance", "previous_balance", "opening_balance", "closing_balance"]
    account_date = statement_anchor_date(importer_json)

    acc_sum = importer_json.get("account_summary") or {}
    for field in date_fields:
        block = acc_sum.get(field)
        if isinstance(block, dict):
            d = block.get("date")
            if d:
                account_date = d
                break

    # Unique statement source ID
    fhash = _file_sha256(source_pdf)
    source_id = _stable16(f"{document_type}|{account_date}|{fhash}")

    rows, meta_rows, acct_rows, balance_rows = [], [], [], []
    transaction_id_occurrences: Dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────
    # Process transactional statements
    # ─────────────────────────────────────────────────────────────
    for holder in importer_json.get("statements", []):
        #
        # 1. NEW — detect explicit parent account
        #
        explicit_parent_id = holder.get("parent_account_id")

        #
        # 2. NEW — detect top-level account_summary parent ID
        #
        summary_parent_id = str(acc_sum.get("account_id"))

        #
        # Decide account_id for this statement
        #
        info = holder.get("cardholder_info") or {}

        has_cardholder = bool(info.get("card_digits") or info.get("card_number"))

        # -------------------------------------------------------
        # Case 1 — Multi-cardholder account (credit cards)
        # -------------------------------------------------------
        if has_cardholder:
            # Always create one account per cardholder
            card_digits = (
                    info.get("card_digits")
                    or (info.get("card_number", "")[-4:] if info.get("card_number") else "unknown")
            )

            account_id = _stable16(f"{institution}|{account_side}|{account_class}|{card_digits}")
            account_number = card_digits
            account_name = f"{institution} {account_class.replace('_', ' ')} {card_digits}".strip()
            parent_mode = False

        # -------------------------------------------------------
        # Case 2 — Pure parent account (HELOC, LOC, loan, single-card CC)
        # -------------------------------------------------------
        elif explicit_parent_id or summary_parent_id:
            account_id = explicit_parent_id or summary_parent_id
            account_number = acc_sum.get("account_number", "")
            account_name = acc_sum.get("account_name", f"{institution} {account_class}")
            parent_mode = True

        # -------------------------------------------------------
        # Case 3 — Fallback (rare)
        # -------------------------------------------------------
        else:
            # Legacy accounts (chequing, cash where digits are unknown)
            account_id = _stable16(f"{institution}|{account_side}|{account_class}|{source_pdf.stem}")
            account_number = acc_sum.get("account_number", "")
            account_name = acc_sum.get("account_name", f"{institution} {account_class}")
            parent_mode = True

        #
        # Register this account (parent or child)
        #
        meta = _asset_meta(account_side, account_class)

        acct_rows.append({
            "account_id": account_id,
            "account_number": account_number,
            "name": account_name,
            "institution": institution,
            "account_side": account_side,
            "account_class": account_class,
            "status": "open",
            # Legacy credit-card behavior only:
            "parent_account_id": holder.get("cardholder_info", {}).get("parent_account_id"),
            "asset_nature": meta.get("asset_nature"),
            "liquidity_class": meta.get("liquidity_class"),
            "base_currency": acc_sum.get("base_currency"),
        })

        #
        # 3. Write all transactions
        #
        for tx in holder.get("transactions", []) or []:
            desc = (tx.get("description") or "").strip()
            currency = _parse_currency(desc)

            debit_val  = _f(tx.get("debit"))
            credit_val = _f(tx.get("credit"))
            raw_val    = _f(tx.get("amount"))
            op_date = tx.get("operation_date")
            tx_type = tx.get("transaction_type", "expense")

            # Signed amount
            if tx_type == "expense":
                signed_amt = -abs(raw_val)
            elif tx_type in ("income", "refund"):
                signed_amt = abs(raw_val)
            elif tx_type == "transfer":
                signed_amt = credit_val - debit_val  # one is always 0
            else:
                signed_amt = raw_val

            is_refund = (tx_type == "refund")

            legacy_tx_id = _stable16(
                f"{source_id}|{account_id}|{op_date}|{desc}|{raw_val}|{tx_type}"
            )
            occurrence = transaction_id_occurrences.get(legacy_tx_id, 0)
            tx_id = (
                legacy_tx_id
                if occurrence == 0
                else _stable16(f"{legacy_tx_id}|occurrence|{occurrence}")
            )
            transaction_id_occurrences[legacy_tx_id] = occurrence + 1

            rows.append({
                "source_statement_id": source_id,
                "document_type": document_type,
                "export_date": export_date,
                "account_date": account_date,
                "institution": institution,
                "account_side": account_side,
                "account_class": account_class,
                "account_id": account_id,
                "account_number": account_number,
                "cardholder_name": None,       # Non-card accounts no longer use cardholder names
                "operation_date": op_date,
                "posted_date": op_date,
                "description": desc,
                "description_norm": normalize_desc(desc),
                "amount_raw": raw_val,
                "is_refund": is_refund,
                "amount": signed_amt,
                "debit": debit_val,
                "credit": credit_val,
                "currency": currency,
                "transaction_type": tx_type,
                "validation_passed": (holder.get("summary", {}) or {}).get("validation_passed"),
                "transaction_id": tx_id,
                "category": "uncategorized",
                "subcategory": DEFAULT_KEY,
                "type": DEFAULT_KEY,
                "fuzzy_score": None,
            })

        #
        # 4. Metadata registry
        #
        meta_rows.append({
            "source_statement_id": source_id,
            "file_sha256": fhash,
            "document_type": document_type,
            "account_date": account_date,
            "institution": institution,
            "account_side": account_side,
            "account_class": account_class,
            "account_number": account_number,
            "account_id": account_id,
            "export_date": export_date,
            "path": str(source_pdf.resolve()),
        })

        #
        # 5. Statement-level closing balance
        #
        summary = holder.get("summary") or {}
        closing = summary.get("closing_balance")
        if closing is not None:
            # Prefer explicit as_of_date, then global account_date,
            # then any top-level account_summary.as_of_date
            asof_candidates = [
                summary.get("as_of_date"),
                account_date,
                acc_sum.get("as_of_date"),
            ]
            asof = next((d for d in asof_candidates if d), None)
            balance_rows.append(
                mb.make_row(
                    account_id=account_id,
                    account_side=account_side,
                    account_class=account_class,
                    balance=float(closing),
                    as_of_date=asof,
                    institution=institution,
                    source_file=source_pdf.name,
                    notes="closing_balance from statement summary",
                )
            )

    # ─────────────────────────────────────────────────────────────
    # NAV-only mode (unchanged)
    # ─────────────────────────────────────────────────────────────
    if not acct_rows and importer_json.get("nav_snapshots"):
        acc = importer_json.get("account_summary", {}) or {}
        fhash = _file_sha256(source_pdf)
        account_id = str(acc.get("account_id") or _stable16(
            f"{institution}|{account_side}|{account_class}|{fhash}"
        ))

        meta = _asset_meta(account_side, account_class)

        acct_rows.append({
            "account_id": account_id,
            "account_number": acc.get("account_number", ""),
            "name": (acc.get("account_name") or f"{institution} {account_class} NAV").strip(),
            "institution": acc.get("institution", institution),
            "account_side": account_side,
            "account_class": account_class,
            "status": "open",
            "asset_nature": meta.get("asset_nature"),
            "liquidity_class": meta.get("liquidity_class"),
            "base_currency": acc_sum.get("base_currency"),
        })

        nav_date = (importer_json.get("nav_snapshots") or [{}])[0].get("date", "")
        meta_rows.append({
            "source_statement_id": source_id,
            "file_sha256": fhash,
            "document_type": (importer_json.get("document_signature") or {}).get("document_type", "NAV_SNAPSHOT"),
            "account_date": nav_date,
            "institution": institution,
            "account_side": account_side,
            "account_class": account_class,
            "account_number": acc.get("account_number", ""),
            "account_id": account_id,
            "export_date": importer_json.get("export_date", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
            "path": str(source_pdf.resolve()),
        })

    # ─────────────────────────────────────────────────────────────
    # Handle top-level account_summary.current_balance
    # ─────────────────────────────────────────────────────────────
    curr = acc_sum.get("current_balance")
    if isinstance(curr, dict) and "amount" in curr:
        bal = float(curr["amount"])
        asof = curr.get("date") or datetime.utcnow().strftime("%Y-%m-%d")
        account_id = str(acc_sum.get("account_id") or _stable16(
            f"{institution}|{account_side}|{account_class}|{source_pdf.stem}"
        ))
        balance_rows.append(
            mb.make_row(
                account_id=account_id,
                account_side=account_side,
                account_class=account_class,
                balance=bal,
                as_of_date=asof,
                institution=institution,
                source_file=source_pdf.name,
                notes="account_summary.current_balance",
            )
        )

        # Ensure parent account is registered
        meta = _asset_meta(account_side, account_class)

        acct_rows.append({
            "account_id": account_id,
            "account_number": acc_sum.get("account_number", ""),
            "name": acc_sum.get("account_name", "Combined Account"),
            "institution": acc_sum.get("institution", institution),
            "account_side": account_side,
            "account_class": account_class,
            "status": "open",
            "asset_nature": meta.get("asset_nature"),
            "liquidity_class": meta.get("liquidity_class"),
            "base_currency": acc_sum.get("base_currency"),
        })

    transactions_df = pd.DataFrame(rows)
    _reconcile_transaction_batch(importer_json, transactions_df)
    _require_unique_transaction_ids(
        transactions_df,
        context="normalized transaction batch",
    )

    return (
        transactions_df,
        pd.DataFrame(meta_rows),
        pd.DataFrame(acct_rows),
        pd.DataFrame(balance_rows),
    )


# ──────────────────────────────────────────────────────────────
# Write parquet partitioned
# ──────────────────────────────────────────────────────────────
def write_partitioned(df: pd.DataFrame, base: Path) -> None:
    if df.empty:
        return
    _require_unique_transaction_ids(df, context="transaction write batch")
    df = df.copy()
    df["operation_date"] = pd.to_datetime(df["operation_date"], errors="coerce")
    df["year"]  = df["operation_date"].dt.year.fillna(-1).astype(int)
    df["month"] = df["operation_date"].dt.month.fillna(-1).astype(int)

    for (y, m), g in df.groupby(["year", "month"], dropna=False):
        part = base / f"year={y}" / f"month={m}"
        _ensure_dir(part)
        outp = part / "transactions.parquet"
        g2 = g.drop(columns=["year", "month"])
        if outp.exists():
            old = pd.read_parquet(outp)
            merged = pd.concat([old, g2], ignore_index=True)
            merged = merged.drop_duplicates(subset=["transaction_id"], keep="last")
            merged.to_parquet(outp, index=False)
        else:
            g2.to_parquet(outp, index=False)

# ──────────────────────────────────────────────────────────────
# NAV snapshots ingestion
# ──────────────────────────────────────────────────────────────
def _ensure_nav_parquet(base: Path) -> Path:
    np = base / "nav_snapshots.parquet"
    if not np.exists():
        df_empty = pd.DataFrame(columns=[
            "account_id","account_name","institution",
            "account_side","account_class","base_currency",
            "nav_date","nav_value","import_source","imported_at"
        ])
        df_empty.to_parquet(np, index=False)
    return np

def _append_nav_parquet(
    base: Path,
    importer_json: Dict[str, Any],
    institution: str,
    account_side: str,
    account_class: str,
    source_pdf: Path,
) -> int:
    nav_rows = []
    acc = importer_json.get("account_summary", {}) or {}
    account_id = acc.get("account_id", "")
    account_name = acc.get("account_name", "")
    base_currency = acc.get("base_currency", "CAD")

    for snap in importer_json.get("nav_snapshots", []):
        nav_rows.append({
            "account_id": account_id,
            "account_name": account_name,
            "institution": acc.get("institution", institution),
            "account_side": account_side,
            "account_class": account_class,
            "operation_date": snap.get("date"),
            "amount": None,
            "debit": None,
            "credit": None,
            "transaction_type": "nav_snapshot",
            "description": "NAV snapshot import",
            "description_norm": "nav snapshot import",
            "nav": float(snap.get("nav", 0.0)),
            "base_currency": base_currency,
            "imported_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "source_file": source_pdf.name,
        })

    if not nav_rows:
        return 0

    df = pd.DataFrame(nav_rows)
    meta_dir = base / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_dir / "nav_snapshots.parquet"

    if path.exists():
        existing = pd.read_parquet(path)
        df = pd.concat([existing, df], ignore_index=True).drop_duplicates(
            subset=["account_id", "operation_date"], keep="last"
        )

    df.to_parquet(path, index=False)
    return len(df)

# ──────────────────────────────────────────────────────────────
# Ingestion orchestrator
# ──────────────────────────────────────────────────────────────
def _delete_statement_transactions(base: Path, source_statement_id: str) -> int:
    """
    Delete all transactions belonging to a statement across all partitions.
    """
    deleted = 0
    for part in base.glob("year=*/month=*"):
        p = part / "transactions.parquet"
        if not p.exists():
            continue

        df = pd.read_parquet(p)
        before = len(df)
        df = df[df["source_statement_id"] != source_statement_id]
        after = len(df)

        if after < before:
            deleted += before - after
            if after == 0:
                p.unlink()
            else:
                df.to_parquet(p, index=False)

    return deleted

def _count_statement_transactions(base: Path, source_statement_id: str) -> int:
    """Count the rows actually persisted for one source statement."""
    total = 0
    for path in base.glob("year=*/month=*/transactions.parquet"):
        statement_ids = pd.read_parquet(
            path,
            columns=["source_statement_id"],
        )["source_statement_id"]
        total += int((statement_ids == source_statement_id).sum())
    return total

def ingest_pdf_with_importer_json(
    importer_json: Dict[str, Any],
    *,
    account_side: str,
    account_class: str,
    institution: str,
    source_pdf_path: Path,
    out_dir: Path
) -> dict:
    overwrite = bool(importer_json.get("_overwrite_statement", False))

    # Normalize to DataFrames
    tx_df, meta_df, acct_df, bal_df = normalize_importer_json(
        importer_json,
        institution=institution,
        account_side=account_side,
        account_class=account_class,
        source_pdf=source_pdf_path,
    )

    # Do not create or mutate storage until every extracted transaction has
    # exactly one non-empty, unique normalized identity.
    _reconcile_transaction_batch(importer_json, tx_df)
    _ensure_dir(out_dir)

    source_id = (
        tx_df["source_statement_id"].iloc[0]
        if not tx_df.empty
        else meta_df["source_statement_id"].iloc[0]
        if not meta_df.empty
        else None
    )

    meta_existing = _load_meta(out_dir)
    existing_ids = set(meta_existing["source_statement_id"].tolist())

    # Already imported?
    if source_id in existing_ids:
        if not overwrite:
            return {
                "written": 0,
                "skipped": True,
                "reason": "already_imported",
                "source_statement_id": source_id,
                "message": "Statement already imported",
            }

        # HARD DELETE
        deleted = _delete_statement_transactions(out_dir, source_id)
    else:
        deleted = 0

    written = 0
    if not tx_df.empty:
        write_partitioned(tx_df, out_dir)
        written = _count_statement_transactions(out_dir, source_id)
        expected = len(tx_df)
        if written != expected:
            raise RuntimeError(
                "Transaction persistence invariant failed: "
                f"expected {expected} rows for the source statement, found {written}"
            )

    nav_written = 0
    if importer_json.get("nav_snapshots"):
        nav_written = _append_nav_parquet(
            base=out_dir,
            importer_json=importer_json,
            institution=institution,
            account_side=account_side,
            account_class=account_class,
            source_pdf=source_pdf_path,
        )

    # Metadata tracking
    meta_existing = _load_meta(out_dir)
    seen = set(meta_existing.get("file_sha256", pd.Series(dtype=str)).tolist())
    fhash = meta_df["file_sha256"].iloc[0] if not meta_df.empty else ""
    new_meta = 0
    if fhash and fhash not in seen and not meta_df.empty:
        _upsert_meta(out_dir, meta_df)
        new_meta = len(meta_df)

    # Account registry
    if not acct_df.empty:
        _upsert_accounts(out_dir, acct_df)

    # NEW — persist statement balances
    if not bal_df.empty:
        mb.upsert(out_dir, bal_df)

    # ─────────────────────────────────────────────
    # FX PREFETCH (non-blocking, deterministic)
    # ─────────────────────────────────────────────
    try:
        from app.services.fx.fetch_yfinance import ensure_fx_rate
        from app.services.fx.resolve import first_business_day_of_month
        from app import config

        settings = config.load_settings()
        display_currency = settings.get("display_currency", "CAD")

        acc = importer_json.get("account_summary") or {}
        base_currency = acc.get("base_currency")

        valuation_date = _extract_valuation_date(importer_json)

        if base_currency and valuation_date:
            fx_anchor = first_business_day_of_month(valuation_date)

            ensure_fx_rate(
                date=fx_anchor,
                from_currency=base_currency,
                to_currency=display_currency,
                parquet_root=out_dir,
            )

            LOG.info(
                "FX prefetched: %s→%s @ %s",
                base_currency,
                display_currency,
                fx_anchor.isoformat(),
            )
        else:
            LOG.info(
                "FX prefetch skipped (missing currency or valuation date)"
            )

    except Exception as e:
        # IMPORTANT: FX must never block import
        LOG.warning("FX prefetch failed (non-fatal): %s", e)

    return {
        "written": written,
        "deleted": deleted,
        "nav_written": nav_written,
        "meta_written": new_meta,
        "accounts_upserted": acct_df["account_id"].nunique() if not acct_df.empty else 0,
        "balances_written": len(bal_df),
        "message": "ok",
    }
