"""
db.py — single place that knows how to connect to Neon Postgres.
Both etl.py and check_alerts.py import this so the connection logic
only exists in one place.
"""

import os
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    """
    Opens a connection to Neon using the DATABASE_URL environment variable.

    Locally: put DATABASE_URL in a .env file (see .env.example) and load it
    with python-dotenv before calling this function.

    In GitHub Actions: DATABASE_URL comes from a repository secret, injected
    as an environment variable in the workflow file — see
    .github/workflows/pipeline.yml
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Set it in your .env file locally, "
            "or as a GitHub Actions secret in CI."
        )
    return psycopg2.connect(db_url)


def run_sql_file(conn, filepath):
    """Runs every statement in a .sql file against the given connection."""
    with open(filepath, "r") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def bulk_upsert(conn, table, columns, rows, conflict_col, update_cols=None):
    """
    Generic upsert helper using psycopg2's execute_values for speed.

    table:        table name, e.g. 'dim_product'
    columns:      list of column names, e.g. ['stock_code', 'description']
    rows:         list of tuples matching the column order
    conflict_col: the column (or comma-separated columns) with the unique
                  constraint to conflict on, e.g. 'stock_code'
    update_cols:  columns to overwrite on conflict; if None, do nothing on conflict
    """
    if not rows:
        return

    col_list = ", ".join(columns)
    if update_cols:
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        conflict_clause = f"ON CONFLICT ({conflict_col}) DO UPDATE SET {set_clause}"
    else:
        conflict_clause = f"ON CONFLICT ({conflict_col}) DO NOTHING"

    query = f"INSERT INTO {table} ({col_list}) VALUES %s {conflict_clause}"

    with conn.cursor() as cur:
        execute_values(cur, query, rows)
    conn.commit()
