# app/cli/cli.py
import argparse
import sys
from pathlib import Path

import pandas as pd
import hashlib

from app.services import queries, categories, update_categories, manual_values
from app.services.normalize_parquet import _upsert_accounts, _stable16, _meta_path
from app import config


# ---------------- Command Handlers ----------------
def cmd_query(args):
    settings = config.load_settings()
    parquet_dir = args.parquet or settings.get("consolidated_statements")
    query_kwargs = {
        "year": args.year,
        "month": args.month,
        "similarity_threshold": args.similarity_threshold or settings.get("similarity_threshold", 80),
        "min_cluster_tx": args.min_cluster_tx or settings.get("min_cluster_tx", 1),
        "single_tx_bucket": args.single_tx_bucket or settings.get("single_tx_bucket", "OTHER_SMALL_CLUSTER"),
    }
    query_kwargs = {k: v for k, v in query_kwargs.items() if v is not None}

    try:
        df = queries.run_query(args.name, parquet_dir, **query_kwargs)
        print(df.to_string(index=False))
    except Exception as e:
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_add_category(args):
    categories.ensure_category(args.category, args.subcategory)
    if args.subcategory:
        print(f"Category '{args.category}' with subcategory '{args.subcategory}' added/ensured.")
    else:
        print(f"Category '{args.category}' added/ensured.")


def cmd_delete_category(args):
    categories.delete_category(args.category)
    print(f"Category '{args.category}' deleted.")


def cmd_delete_subcategory(args):
    categories.delete_subcategory(args.category, args.subcategory)
    print(f"Subcategory '{args.subcategory}' under '{args.category}' deleted.")


def cmd_list_categories(args):
    cats = categories.list_categories()
    if not cats:
        print("No categories defined.")
    else:
        for cat, subcats in cats.items():
            if not subcats:
                print(f"- {cat}")
            else:
                for subcat in subcats.keys():
                    print(f"- {cat}/{subcat}")


def cmd_add_seed(args):
    descs = args.descriptions or []
    for i, tx_id in enumerate(args.tx_ids):
        desc = descs[i] if i < len(descs) else ""
        categories.add_seed_transaction(args.category, args.subcategory, tx_id, desc)
    print(f"Added {len(args.tx_ids)} seeds to {args.category}/{args.subcategory}")


def cmd_remove_seed(args):
    categories.remove_seed_transaction(args.category, args.subcategory, args.tx_id)
    print(f"Removed seed {args.tx_id} from {args.category}/{args.subcategory}")


def cmd_update_categories(args):
    """CLI handler to update categories in parquet files using seeds and fuzzy matching."""
    settings = config.load_settings()
    # update_categories.update_categories() requires a Path (it calls
    # collect_parquet_files(), which calls .is_file() on it) -- settings
    # values and --parquet are always plain strings (BUG-05 regression).
    parquet_dir = Path(args.parquet or settings.get("consolidated_statements"))

    try:
        update_categories.update_categories(
            parquet_dir,
            threshold=args.threshold,
            all_tx=args.all_tx,
            year=args.year,
            uncategorized=args.uncategorized,
            unsubcategorized=args.unsubcategorized
        )
        print("Categories updated successfully.")
    except Exception as e:
        print(f"Update failed: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------- Manual accounts & values ----------------
def cmd_create_manual_account(args):
    settings = config.load_settings()
    root = config.Path(settings["consolidated_statements"]).resolve()

    account_id = _stable16(f"{args.institution}|{args.account_side}|{args.account_class}|{args.name}")

    row = pd.DataFrame([{
        "account_id": account_id,
        "account_number": args.account_number or "",
        "name": args.name,
        "institution": args.institution or "MANUAL",
        "account_side": args.account_side,
        "account_class": args.account_class,
        "status": "open",
    }])

    _upsert_accounts(root, row)
    print(f"Created manual account {account_id} ({args.account_side}/{args.account_class})")


def cmd_add_manual_value(args):
    settings = config.load_settings()
    root = config.Path(settings["consolidated_statements"]).resolve()

    tx_id = hashlib.sha256(
        f"{args.account_id}|{args.date}|{args.amount}|{args.note or 'manual'}".encode()
    ).hexdigest()[:16]

    df = pd.DataFrame([{
        "account_id": args.account_id,
        "date": args.date,
        "month": pd.to_datetime(args.date).strftime("%Y-%m"),
        "amount": float(args.amount),
        "description": args.note or "manual transaction",
        "transaction_id": tx_id,
        "transaction_type": "manual_value",
        "currency": "CAD",
        "category": "uncategorized",
        "subcategory": "__default__",
        "type": "__default__",
        "fuzzy_score": None,
    }])

    manual_values.upsert_values(root, df)
    print(f"Added manual value {args.amount} on {args.date} for account {args.account_id}")


# ---------------- Main Entry ----------------
def run_cli(argv=None):
    parser = argparse.ArgumentParser(description="CLEAR CLI")
    subparsers = parser.add_subparsers(dest="command")

    # --- Query ---
    query_parser = subparsers.add_parser("query", help="Run queries on consolidated Parquet files")
    query_parser.add_argument("--parquet", type=str, help="Parquet directory")
    query_parser.add_argument("--name", required=True, choices=list(queries.QUERY_MAP.keys()), help="Query name")
    query_parser.add_argument("--year", type=int, help="Year (if query requires)")
    query_parser.add_argument("--month", type=int, help="Month (if query requires)")
    query_parser.add_argument("--similarity_threshold", type=int, help="For fuzzy merchant query")
    query_parser.add_argument("--min_cluster_tx", type=int, help="For fuzzy merchant query")
    query_parser.add_argument("--single_tx_bucket", type=str, help="Bucket name for small clusters")
    query_parser.set_defaults(func=cmd_query)

    # --- Category management ---
    add_cat_parser = subparsers.add_parser("add_category", help="Add a category (and optional subcategory)")
    add_cat_parser.add_argument("--category", required=True)
    add_cat_parser.add_argument("--subcategory")
    add_cat_parser.set_defaults(func=cmd_add_category)

    del_cat_parser = subparsers.add_parser("delete_category", help="Delete a category")
    del_cat_parser.add_argument("--category", required=True)
    del_cat_parser.set_defaults(func=cmd_delete_category)

    del_subcat_parser = subparsers.add_parser("delete_subcategory", help="Delete a subcategory under a category")
    del_subcat_parser.add_argument("--category", required=True)
    del_subcat_parser.add_argument("--subcategory", required=True)
    del_subcat_parser.set_defaults(func=cmd_delete_subcategory)

    list_cat_parser = subparsers.add_parser("list_categories", help="List categories and subcategories")
    list_cat_parser.set_defaults(func=cmd_list_categories)

    # --- Seed management ---
    seed_parser = subparsers.add_parser("add_seed", help="Add seeds to a category/subcategory")
    seed_parser.add_argument("--category", required=True)
    seed_parser.add_argument("--subcategory", default="__default__")
    seed_parser.add_argument("--tx_ids", nargs="+", required=True)
    seed_parser.add_argument("--descriptions", nargs="*")
    seed_parser.set_defaults(func=cmd_add_seed)

    remove_seed_parser = subparsers.add_parser("remove_seed", help="Remove a seed transaction")
    remove_seed_parser.add_argument("--category", required=True)
    remove_seed_parser.add_argument("--subcategory", required=True)
    remove_seed_parser.add_argument("--tx_id", required=True)
    remove_seed_parser.set_defaults(func=cmd_remove_seed)

    # --- Update categories ---
    update_parser = subparsers.add_parser("update_categories", help="Update categories in Parquet using seeds and fuzzy matching")
    update_parser.add_argument("--parquet", type=str, help="Parquet directory")
    update_parser.add_argument("--threshold", type=int, default=80, help="Fuzzy match threshold (0-100)")
    update_parser.add_argument("--all_tx", action="store_true", help="Update all transactions, including already categorized")
    update_parser.add_argument("--uncategorized", action="store_true", help="Only update uncategorized transactions")
    update_parser.add_argument("--unsubcategorized", action="store_true", help="Only update transactions with subcategory '__default__'")
    update_parser.add_argument("--year", type=int, help="Limit updates to a specific year")
    update_parser.set_defaults(func=cmd_update_categories)

    # --- Manual accounts ---
    acct_parser = subparsers.add_parser("create_manual_account", help="Create a manual account (e.g., house, car, loan)")
    acct_parser.add_argument("--name", required=True)
    acct_parser.add_argument("--account_side", required=True, choices=config.ACCOUNT_SIDES)
    acct_parser.add_argument("--account_class", required=True, help="e.g., home_fmv, car_fmv, other_debt")
    acct_parser.add_argument("--institution", default="MANUAL")
    acct_parser.add_argument("--account_number", default="")
    acct_parser.set_defaults(func=cmd_create_manual_account)

    # --- Manual values ---
    value_parser = subparsers.add_parser("add_manual_value", help="Add a manual value to an account")
    value_parser.add_argument("--account_id", required=True)
    value_parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    value_parser.add_argument("--amount", required=True, type=float)
    value_parser.add_argument("--note", default="")
    value_parser.set_defaults(func=cmd_add_manual_value)

    # --- Parse and dispatch ---
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)

"""
# Examples:

# Run a query
python run.py query --name total_spending_by_cardholder --year 2025 --month 8

# Add a category
python run.py add_category --category lifestyle

# Add a subcategory
python run.py add_category --category lifestyle --subcategory restaurant

# List categories
python run.py list_categories

# Add seeds
python run.py add_seed --category lifestyle --subcategory restaurant --tx_ids tx1001 tx1002 --descriptions "McDonalds" "Starbucks"

# Remove a seed
python run.py remove_seed --category lifestyle --subcategory restaurant --tx_id tx1001

# Delete category
python run.py delete_category --category lifestyle

# Delete a subcategory
python run.py delete_subcategory --category lifestyle --subcategory restaurant

# Update all transactions
python run.py update_categories --all_tx

# Update only uncategorized transactions for 2025
python run.py update_categories --uncategorized --year 2025

# Create a manual account (asset: home)
python run.py create_manual_account --name "Primary Residence" --account_side asset --account_class home_fmv --institution MyBank

# Create a manual liability account
python run.py create_manual_account --name "Car Loan" --account_side liability --account_class other_debt

# Add a manual value
python run.py add_manual_value --account_id <the_id> --date 2025-09-20 --amount 250000 --note "Zillow estimate"

"""
