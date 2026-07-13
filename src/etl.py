"""
etl.py — reads the Online Retail II dataset, cleans it, and loads it into
the star schema in Neon Postgres.

INCREMENTAL REPLAY — this is what makes a static historical file behave
like an ongoing data feed:
Rather than reloading the whole dataset every run (which would make every
run identical and pointless to automate), each run loads the *next slice*
of the historical timeline into fact_sales — controlled by CHUNK_DAYS
(default 30, i.e. roughly a month of transactions per run) — picking up
exactly where the previous run left off, tracked in the etl_control table.
Dimension tables (product/customer/date/location) are still safely
re-upserted from the full file every run, since that's cheap and avoids
missing a dimension row a later chunk needs.
When the replay reaches the end of the dataset's real date range, it wraps
around and starts again from the earliest date — so the pipeline can run
indefinitely without you touching it, which is the point of a demo like this.
"""

import os
import glob
import random
import pandas as pd
from datetime import timedelta

from db import get_connection, bulk_upsert
from psycopg2.extras import execute_values

random.seed(42)  # reproducible synthetic costs across runs

DATA_PATH = os.environ.get("DATA_PATH", "data/online_retail_ii.xlsx")
CHUNK_DAYS = int(os.environ.get("CHUNK_DAYS", "30"))


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

    return df


# ---------------------------------------------------------
# 3. Enrich (simulated cost/profit)
# ---------------------------------------------------------
def add_simulated_cost(df):
    """
    The Online Retail II dataset has sale price but no cost data, so there is
    no real profit figure to compute. To demonstrate the profitability
    analysis this project is built around, we simulate a unit cost as a
    random 40%-70% of the sale price (a plausible retail margin band).
    This is disclosed clearly in the README and in any presentation of the
    project — it is a stand-in for real cost data, not a real figure.
    """
    margin_factor = [random.uniform(0.4, 0.7) for _ in range(len(df))]
    df["unit_cost"] = (df["price"] * pd.Series(margin_factor, index=df.index)).round(2)
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
# 5. Work out which slice of history to load this run
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
        print(f"First run — starting replay from {start_date}")
    elif last_loaded_date >= max_date:
        start_date = min_date
        print(f"Reached the end of the dataset ({max_date}) — "
              f"wrapping around and restarting replay from {start_date}")
    else:
        start_date = last_loaded_date + timedelta(days=1)

    end_date = min(start_date + timedelta(days=chunk_days - 1), max_date)

    print(f"Loading transactions from {start_date} to {end_date} "
          f"({chunk_days}-day chunk)")

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
# 6. Load the chunk's fact rows (append-only — nothing is truncated)
# ---------------------------------------------------------
def load_fact_chunk(conn, chunk, location_map):
    if chunk.empty:
        print("This chunk has no rows (unlikely, but nothing to load).")
        return

    chunk["location_id"] = chunk["country"].map(location_map)
    chunk["date_id"] = chunk["invoice_date"].dt.date

    fact_rows = list(chunk[[
        "invoice_no", "stock_code", "customer_id", "date_id", "location_id",
        "quantity", "price", "unit_cost", "revenue", "profit", "is_return"
    ]].itertuples(index=False, name=None))

    print(f"Inserting {len(fact_rows)} fact_sales rows for this run...")
    with conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO fact_sales
               (invoice_no, stock_code, customer_id, date_id, location_id,
                quantity, unit_price, unit_cost, revenue, profit, is_return)
               VALUES %s""",
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
    load_fact_chunk(conn, chunk, location_map)
    update_control(conn, end_date)

    conn.close()
    print("ETL run complete.")


if __name__ == "__main__":
    main()

