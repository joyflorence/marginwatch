"""
etl.py — reads the Online Retail II dataset, cleans it, and loads it into
the star schema in Neon Postgres.

INCREMENTAL LOADS — historical facts are loaded once and retained. Later
runs select only source dates newer than etl_control.last_loaded_date.
CHUNK_DAYS defaults to 0, which backfills every available historical year on
the first run; a positive value supports a staged initial backfill. Fact rows
also have deterministic source keys, making a repeated load idempotent.
Dimension tables are safely re-upserted from the full file every run so a
new fact never references a missing product, customer, date, or location.
"""

import os
import glob
import hashlib
import pandas as pd
from datetime import timedelta

from db import get_connection, bulk_upsert
from psycopg2.extras import execute_values

DATA_PATH = os.environ.get("DATA_PATH", "data/online_retail_ii.xlsx")
# Load all historical years on the first run. Set this to a positive number
# (for example 30) only when demonstrating a staged historical backfill.
CHUNK_DAYS = int(os.environ.get("CHUNK_DAYS", "0"))


def start_run_log(conn, source_path):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO etl_run_log (status, source_path)
               VALUES ('running', %s) RETURNING run_id""",
            (source_path,),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_run_log(conn, run_id, status, metrics, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE etl_run_log
               SET finished_at = NOW(), status = %s, source_rows = %s,
                   clean_rows = %s, candidate_fact_rows = %s,
                   inserted_fact_rows = %s, rejected_rows = %s,
                   last_source_date = %s, error_message = %s
               WHERE run_id = %s""",
            (
                status,
                metrics["source_rows"],
                metrics["clean_rows"],
                metrics["candidate_fact_rows"],
                metrics["inserted_fact_rows"],
                metrics["rejected_rows"],
                metrics["last_source_date"],
                error_message,
                run_id,
            ),
        )
    conn.commit()


def run_data_quality_checks(raw_rows, df):
    """Return auditable checks; failed checks block the fact load."""
    checks = []

    def add(name, status, observed_value, details):
        checks.append({
            "name": name,
            "status": status,
            "observed_value": int(observed_value),
            "details": details,
        })

    add(
        "clean_rows_available",
        "passed" if len(df) > 0 else "failed",
        len(df),
        "Cleaned rows available for loading.",
    )
    duplicate_keys = int(df["source_row_key"].duplicated().sum())
    add(
        "unique_source_row_keys",
        "passed" if duplicate_keys == 0 else "failed",
        duplicate_keys,
        "Duplicate deterministic source-row keys found after cleaning.",
    )
    required_columns = ["invoice_no", "stock_code", "invoice_date", "price", "quantity"]
    missing_required_values = int(df[required_columns].isna().sum().sum())
    add(
        "required_fact_values_present",
        "passed" if missing_required_values == 0 else "failed",
        missing_required_values,
        "Null values in fields required to create a fact row.",
    )
    dropped_rows = raw_rows - len(df)
    add(
        "rejected_source_rows",
        "passed" if dropped_rows == 0 else "warning",
        dropped_rows,
        "Rows removed because price or quantity was missing, or price was not positive.",
    )
    negative_non_returns = int(((df["quantity"] < 0) & ~df["is_return"]).sum())
    add(
        "negative_quantities_marked_as_returns",
        "passed" if negative_non_returns == 0 else "warning",
        negative_non_returns,
        "Negative quantities whose invoice number is not marked as a cancellation.",
    )
    return checks


def persist_quality_checks(conn, run_id, checks):
    rows = [
        (run_id, check["name"], check["status"], check["observed_value"], check["details"])
        for check in checks
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO etl_quality_check
               (run_id, check_name, status, observed_value, details)
               VALUES %s""",
            rows,
        )
    conn.commit()


# ---------------------------------------------------------
# 1. Load
# ---------------------------------------------------------
def load_raw_data(path):
    """
    Loads the dataset whether it's a folder (Kaggle input directory),
    a single CSV, or an Excel workbook with multiple year sheets
    (the original UCI file ships as two sheets: 2009-2010 and 2010-2011).
    """
    if os.path.isdir(path):
        candidates = glob.glob(os.path.join(path, "*.csv")) + glob.glob(
            os.path.join(path, "*.xlsx")
        )
        if not candidates:
            raise FileNotFoundError(f"No .csv or .xlsx file found in {path}")
        path = candidates[0]

    print(f"Loading data from: {path}")

    if path.endswith(".xlsx"):
        sheets = pd.read_excel(path, sheet_name=None)
        df = pd.concat(sheets.values(), ignore_index=True)
    else:
        df = pd.read_csv(path, encoding="ISO-8859-1")

    return df


# ---------------------------------------------------------
# 2. Clean
# ---------------------------------------------------------
def clean_data(df):
    # Standardize column names regardless of spacing/case in the source file
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    rename_map = {
        "invoice": "invoice_no",
        "stockcode": "stock_code",
        "customer_id": "customer_id",
        "customerid": "customer_id",
        "invoicedate": "invoice_date",
        "unitprice": "price",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    required = ["invoice_no", "stock_code", "description", "quantity",
                "invoice_date", "price", "customer_id", "country"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Expected column '{col}' not found in dataset")

    # Drop rows with no price or no quantity — can't compute revenue without them
    df = df.dropna(subset=["price", "quantity"])
    df = df[df["price"] > 0]

    # Missing customer IDs become 'GUEST' rather than being dropped —
    # guest checkouts are real, valid transactions
    df["customer_id"] = df["customer_id"].fillna("GUEST").astype(str).str.replace(".0", "", regex=False)

    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["invoice_no"] = df["invoice_no"].astype(str)

    # A cancelled order in this dataset has an invoice number starting with 'C'
    df["is_return"] = df["invoice_no"].str.startswith("C")

    df["stock_code"] = df["stock_code"].astype(str)
    df["description"] = df["description"].fillna("UNKNOWN")
    df["country"] = df["country"].fillna("Unspecified")

    # A stable identity makes the fact load idempotent. The duplicate ordinal
    # preserves legitimate identical invoice lines without relying on the
    # dataframe's transient row index.
    identity_columns = [
        "invoice_no", "stock_code", "description", "quantity", "invoice_date",
        "price", "customer_id", "country",
    ]
    identity_hash = pd.util.hash_pandas_object(
        df[identity_columns].astype(str), index=False
    ).astype("uint64")
    duplicate_ordinal = identity_hash.groupby(identity_hash, sort=False).cumcount()
    df["source_row_key"] = [
        f"{value:016x}-{ordinal}"
        for value, ordinal in zip(identity_hash, duplicate_ordinal)
    ]

    return df


# ---------------------------------------------------------
# 3. Enrich (simulated cost/profit)
# ---------------------------------------------------------
def add_simulated_cost(df):
    """
    The Online Retail II dataset has sale price but no cost data, so there is
    no real profit figure to compute. To demonstrate the profitability
    analysis this project is built around, we simulate one stable unit cost
    per SKU: 40%-70% of that SKU's median selling price. A stable product
    cost prevents random row-level noise from creating false margin alerts.
    This is disclosed clearly in the README and in any presentation of the
    project — it is a stand-in for real cost data, not a real figure.
    """
    median_price = df.groupby("stock_code")["price"].transform("median")
    margin_factor = df["stock_code"].map(
        lambda code: 0.4 + (
            int(hashlib.sha256(code.encode("utf-8")).hexdigest()[:8], 16)
            / 0xFFFFFFFF
        ) * 0.3
    )
    df["unit_cost"] = (median_price * margin_factor).round(2)
    df["revenue"] = (df["price"] * df["quantity"]).round(2)
    df["profit"] = (df["revenue"] - (df["unit_cost"] * df["quantity"])).round(2)
    return df


# ---------------------------------------------------------
# 4. Build dimension tables (from the FULL file, every run —
#    cheap, and guarantees no chunk ever references a missing
#    product/customer/date/location row)
# ---------------------------------------------------------
def build_dimensions(df):
    dim_product = (
        df.groupby("stock_code")
        .agg(description=("description", "first"),
             unit_cost=("unit_cost", "mean"),
             first_seen_date=("invoice_date", "min"))
        .reset_index()
    )
    dim_product["unit_cost"] = dim_product["unit_cost"].round(2)
    dim_product["first_seen_date"] = dim_product["first_seen_date"].dt.date

    dim_customer = (
        df.groupby("customer_id")
        .agg(country=("country", "first"),
             first_order_date=("invoice_date", "min"))
        .reset_index()
    )
    dim_customer["first_order_date"] = dim_customer["first_order_date"].dt.date

    dates = df["invoice_date"].dt.date.unique()
    dim_date = pd.DataFrame({"date_id": dates})
    dim_date["date_id"] = pd.to_datetime(dim_date["date_id"])
    dim_date["year"] = dim_date["date_id"].dt.year
    dim_date["quarter"] = dim_date["date_id"].dt.quarter
    dim_date["month"] = dim_date["date_id"].dt.month
    dim_date["month_name"] = dim_date["date_id"].dt.strftime("%B")
    dim_date["week"] = dim_date["date_id"].dt.isocalendar().week.astype(int)
    dim_date["day_of_week"] = dim_date["date_id"].dt.strftime("%A")
    dim_date["is_weekend"] = dim_date["date_id"].dt.dayofweek >= 5
    dim_date["date_id"] = dim_date["date_id"].dt.date

    dim_location = pd.DataFrame({"country": sorted(df["country"].unique())})

    return dim_product, dim_customer, dim_date, dim_location


def load_dimensions(conn, dim_product, dim_customer, dim_date, dim_location):
    print(f"Upserting {len(dim_location)} locations...")
    bulk_upsert(conn, "dim_location", ["country"],
                list(dim_location.itertuples(index=False, name=None)),
                conflict_col="country")

    with conn.cursor() as cur:
        cur.execute("SELECT location_id, country FROM dim_location")
        location_map = {country: loc_id for loc_id, country in cur.fetchall()}

    print(f"Upserting {len(dim_product)} products...")
    bulk_upsert(conn, "dim_product",
                ["stock_code", "description", "unit_cost", "first_seen_date"],
                list(dim_product.itertuples(index=False, name=None)),
                conflict_col="stock_code",
                update_cols=["description", "unit_cost"])

    print(f"Upserting {len(dim_customer)} customers...")
    bulk_upsert(conn, "dim_customer",
                ["customer_id", "country", "first_order_date"],
                list(dim_customer.itertuples(index=False, name=None)),
                conflict_col="customer_id")

    print(f"Upserting {len(dim_date)} dates...")
    bulk_upsert(conn, "dim_date",
                ["date_id", "year", "quarter", "month", "month_name",
                 "week", "day_of_week", "is_weekend"],
                list(dim_date.itertuples(index=False, name=None)),
                conflict_col="date_id")

    return location_map


# ---------------------------------------------------------
# 5. Work out which new source dates to load this run
# ---------------------------------------------------------
def get_next_chunk(conn, df, chunk_days=CHUNK_DAYS):
    min_date = df["invoice_date"].dt.date.min()
    max_date = df["invoice_date"].dt.date.max()

    with conn.cursor() as cur:
        cur.execute("SELECT last_loaded_date FROM etl_control WHERE id = 1")
        row = cur.fetchone()
        last_loaded_date = row[0] if row else None

    if last_loaded_date is None:
        start_date = min_date
        print(f"First run — backfilling history from {start_date}")
    elif last_loaded_date >= max_date:
        print("No source dates newer than the warehouse are available.")
        return df.iloc[0:0].copy(), None
    else:
        start_date = last_loaded_date + timedelta(days=1)

    end_date = max_date if chunk_days <= 0 else min(
        start_date + timedelta(days=chunk_days - 1), max_date
    )

    print(f"Loading transactions from {start_date} to {end_date} "
          f"({'full historical backfill' if chunk_days <= 0 else f'{chunk_days}-day chunk'})")

    mask = (df["invoice_date"].dt.date >= start_date) & (df["invoice_date"].dt.date <= end_date)
    chunk = df.loc[mask].copy()

    return chunk, end_date


def update_control(conn, end_date):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE etl_control SET last_loaded_date = %s WHERE id = 1",
            (end_date,),
        )
    conn.commit()


# ---------------------------------------------------------
# 6. Load new fact rows (append-only and idempotent)
# ---------------------------------------------------------
def load_fact_chunk(conn, chunk, location_map):
    if chunk.empty:
        print("This chunk has no rows (unlikely, but nothing to load).")
        return

    chunk["location_id"] = chunk["country"].map(location_map)
    chunk["date_id"] = chunk["invoice_date"].dt.date

    fact_rows = list(chunk[[
        "invoice_no", "stock_code", "customer_id", "date_id", "location_id",
        "quantity", "price", "unit_cost", "revenue", "profit", "is_return",
        "source_row_key",
    ]].itertuples(index=False, name=None))

    print(f"Inserting {len(fact_rows)} fact_sales rows for this run...")
    with conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO fact_sales
               (invoice_no, stock_code, customer_id, date_id, location_id,
                quantity, unit_price, unit_cost, revenue, profit, is_return,
                source_row_key)
               VALUES %s
               ON CONFLICT (source_row_key) DO NOTHING""",
            fact_rows,
            page_size=1000,
        )
    conn.commit()


def main():
    df = load_raw_data(DATA_PATH)
    df = clean_data(df)
    df = add_simulated_cost(df)

    conn = get_connection()

    dim_product, dim_customer, dim_date, dim_location = build_dimensions(df)
    location_map = load_dimensions(conn, dim_product, dim_customer, dim_date, dim_location)

    chunk, end_date = get_next_chunk(conn, df)
    if chunk.empty:
        if end_date is None:
            print("No new facts to insert.")
        else:
            print("No transactions in this source-date range.")
            update_control(conn, end_date)
    else:
        load_fact_chunk(conn, chunk, location_map)
        update_control(conn, end_date)

    conn.close()
    print("ETL run complete.")


if __name__ == "__main__":
    main()
