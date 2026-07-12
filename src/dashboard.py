"""
dashboard.py — Streamlit dashboard reading live from the Neon warehouse.

Run locally with: streamlit run src/dashboard.py
Deployed for free on Streamlit Community Cloud (see README for steps).
"""

import os
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine

st.set_page_config(page_title="Retail Decision Dashboard", layout="wide")


@st.cache_resource
def get_engine():
    db_url = os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL")
    if not db_url:
        st.error("DATABASE_URL not found in environment or Streamlit secrets.")
        st.stop()
    return create_engine(db_url)


@st.cache_data(ttl=300)  # refresh every 5 minutes, avoids hammering the DB
def run_query(sql):
    return pd.read_sql(sql, get_engine())


st.title("Retail Decision Dashboard")
st.caption("Live view of the star schema in the database — refreshes automatically after every pipeline run.")

# ---- KPI row ----
kpis = run_query("""
    SELECT
        SUM(revenue) FILTER (WHERE NOT is_return) AS total_revenue,
        SUM(profit)  FILTER (WHERE NOT is_return) AS total_profit,
        COUNT(DISTINCT invoice_no) AS total_orders,
        COUNT(DISTINCT customer_id) AS total_customers
    FROM fact_sales
""").iloc[0]

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Revenue", f"£{kpis['total_revenue']:,.0f}")
col2.metric("Total Profit", f"£{kpis['total_profit']:,.0f}")
col3.metric("Orders", f"{kpis['total_orders']:,}")
col4.metric("Customers", f"{kpis['total_customers']:,}")

st.divider()

# ---- Revenue trend ----
st.subheader("Revenue Over Time")
trend = run_query("""
    SELECT date_id AS date, SUM(revenue) AS revenue
    FROM fact_sales
    WHERE NOT is_return
    GROUP BY date_id
    ORDER BY date_id
""")
st.line_chart(trend.set_index("date"))

col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Top 10 Products by Profit")
    top_products = run_query("""
        SELECT p.description, SUM(f.profit) AS profit
        FROM fact_sales f
        JOIN dim_product p ON p.stock_code = f.stock_code
        WHERE NOT f.is_return
        GROUP BY p.description
        ORDER BY profit DESC
        LIMIT 10
    """)
    st.bar_chart(top_products.set_index("description"))

with col_b:
    st.subheader("Revenue by Country")
    by_country = run_query("""
        SELECT l.country, SUM(f.revenue) AS revenue
        FROM fact_sales f
        JOIN dim_location l ON l.location_id = f.location_id
        WHERE NOT f.is_return
        GROUP BY l.country
        ORDER BY revenue DESC
        LIMIT 10
    """)
    st.bar_chart(by_country.set_index("country"))

st.divider()

# ---- Alert history ----
st.subheader("Recent Automated Alerts")
alerts = run_query("""
    SELECT triggered_at, stock_code, message
    FROM alert_log
    ORDER BY triggered_at DESC
    LIMIT 10
""")
if alerts.empty:
    st.info("No alerts have fired yet.")
else:
    st.dataframe(alerts, use_container_width=True)