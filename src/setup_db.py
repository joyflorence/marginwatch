"""
setup_db.py — run this ONCE to create the star schema in your Neon database.
Usage: python setup_db.py
Re-running it is safe; schema.sql drops and recreates all tables, so any
data loaded so far will be wiped — that's expected for a one-time setup step.
"""

from db import get_connection, run_sql_file

if __name__ == "__main__":
    conn = get_connection()
    run_sql_file(conn, "sql/schema.sql")
    conn.close()
    print("Schema created successfully.")
    
