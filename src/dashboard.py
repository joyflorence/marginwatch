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
st.caption("Live view of the data from the warehouse — refreshes automatically after every pipeline run.")

# ---- KPI row ----
kpis = run_query("""
    SELECT
        SUM(revenue) FILTER (WHERE NOT is_return) AS total_revenue,
        SUM(profit)  FILTER (WHERE NOT is_return) AS total_profit,
        COUNT(DISTINCT invoice_no) FILTER (WHERE NOT is_return) AS total_orders,
        COUNT(DISTINCT customer_id) AS total_customers,
        COUNT(*) FILTER (WHERE is_return) AS return_lines,
        COUNT(*) AS total_lines,
        COUNT(DISTINCT invoice_no) FILTER (WHERE customer_id = 'GUEST') AS guest_orders
    FROM fact_sales
""").iloc[0]

margin_pct = (kpis["total_profit"] / kpis["total_revenue"] * 100) if kpis["total_revenue"] else 0
aov = (kpis["total_revenue"] / kpis["total_orders"]) if kpis["total_orders"] else 0
return_rate = (kpis["return_lines"] / kpis["total_lines"] * 100) if kpis["total_lines"] else 0
guest_share = (kpis["guest_orders"] / kpis["total_orders"] * 100) if kpis["total_orders"] else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Revenue", f"£{kpis['total_revenue']:,.0f}")
col2.metric("Total Profit", f"£{kpis['total_profit']:,.0f}")
col3.metric("Orders", f"{kpis['total_orders']:,}")
col4.metric("Customers", f"{kpis['total_customers']:,}")

col5, col6, col7, col8 = st.columns(4)
col5.metric("Profit Margin", f"{margin_pct:.1f}%")
col6.metric("Avg Order Value", f"£{aov:,.2f}")
col7.metric("Return Rate", f"{return_rate:.1f}%")
col8.metric("Guest Checkout Share", f"{guest_share:.1f}%")

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
    st.subheader("Top 10 Customers by Revenue")
    top_customers = run_query("""
        SELECT customer_id, SUM(revenue) AS revenue
        FROM fact_sales
        WHERE NOT is_return AND customer_id != 'GUEST'
        GROUP BY customer_id
        ORDER BY revenue DESC
        LIMIT 10
    """)
    st.bar_chart(top_customers.set_index("customer_id"))

st.divider()

col_c, col_d = st.columns(2)

with col_c:
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

with col_d:
    st.subheader("Returns Over Time")
    returns_trend = run_query("""
        SELECT date_id AS date, COUNT(*) AS return_lines
        FROM fact_sales
        WHERE is_return
        GROUP BY date_id
        ORDER BY date_id
    """)
    if returns_trend.empty:
        st.info("No returns recorded yet in the loaded data.")
    else:
        st.line_chart(returns_trend.set_index("date"))

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