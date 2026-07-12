"""
etl.py — reads the Online Retail II dataset, cleans it, and loads it into
the star schema in Neon Postgres.

Two ways to point this at your data, controlled by the DATA_PATH env var:

1. Running inside a Kaggle notebook (exploration / first test run):
   DATA_PATH=/kaggle/input/datasets/cgrymn/online-retail-ii-uci-dataset

2. Running in GitHub Actions (the automated, scheduled version):
   Kaggle's /kaggle/input path does not exist on a GitHub Actions runner.
   Export the cleaned/raw file from Kaggle once, commit it to this repo
   under data/online_retail_ii.xlsx, and set:
   DATA_PATH=data/online_retail_ii.xlsx
   (see README.md, "Getting the dataset out of Kaggle" section)

This script is idempotent: every run truncates and reloads all tables.
That's a deliberate simplification for a portfolio project — it avoids
duplicate-row logic and keeps the warehouse always in sync with the
current source file. A production system would load incrementally instead.
"""

import os
import glob
import random
import pandas as pd
from datetime import datetime

from db import get_connection, bulk_upsert

random.seed(42)  # reproducible synthetic costs across runs

DATA_PATH = os.environ.get("DATA_PATH", "data/online_retail_ii.xlsx")
print("DATA_PATH =", DATA_PATH)
print("Exists =", os.path.exists(DATA_PATH))


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
    IMPORTANT — read this before presenting the project:
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
# 4. Build dimension tables
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


# ---------------------------------------------------------
# 5. Load everything into Postgres
# ---------------------------------------------------------
def load_to_warehouse(df, dim_product, dim_customer, dim_date, dim_location):
    conn = get_connection()

    print("Truncating existing tables (idempotent reload)...")
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE fact_sales, dim_product, dim_customer, dim_date, dim_location RESTART IDENTITY CASCADE"
        )
    conn.commit()

    print(f"Loading {len(dim_location)} locations...")
    bulk_upsert(conn, "dim_location", ["country"],
                list(dim_location.itertuples(index=False, name=None)),
                conflict_col="country")

    # Pull generated location_ids back so fact_sales can reference them
    with conn.cursor() as cur:
        cur.execute("SELECT location_id, country FROM dim_location")
        location_map = {country: loc_id for loc_id, country in cur.fetchall()}

    print(f"Loading {len(dim_product)} products...")
    bulk_upsert(conn, "dim_product",
                ["stock_code", "description", "unit_cost", "first_seen_date"],
                list(dim_product.itertuples(index=False, name=None)),
                conflict_col="stock_code")

    print(f"Loading {len(dim_customer)} customers...")
    bulk_upsert(conn, "dim_customer",
                ["customer_id", "country", "first_order_date"],
                list(dim_customer.itertuples(index=False, name=None)),
                conflict_col="customer_id")

    print(f"Loading {len(dim_date)} dates...")
    bulk_upsert(conn, "dim_date",
                ["date_id", "year", "quarter", "month", "month_name",
                 "week", "day_of_week", "is_weekend"],
                list(dim_date.itertuples(index=False, name=None)),
                conflict_col="date_id")

    print(f"Loading {len(df)} fact_sales rows...")
    df["location_id"] = df["country"].map(location_map)
    df["date_id"] = df["invoice_date"].dt.date

    fact_rows = list(df[[
        "invoice_no", "stock_code", "customer_id", "date_id", "location_id",
        "quantity", "price", "unit_cost", "revenue", "profit", "is_return"
    ]].itertuples(index=False, name=None))

    with conn.cursor() as cur:
        from psycopg2.extras import execute_values
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
    conn.close()
    print("ETL run complete.")


def main():
    df = load_raw_data(DATA_PATH)
    df = clean_data(df)
    df = add_simulated_cost(df)
    dim_product, dim_customer, dim_date, dim_location = build_dimensions(df)
    load_to_warehouse(df, dim_product, dim_customer, dim_date, dim_location)


if __name__ == "__main__":
    main()
