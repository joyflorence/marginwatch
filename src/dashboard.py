"""
dashboard.py — Streamlit dashboard reading live from the Neon warehouse.

Run locally with: streamlit run src/dashboard.py
"""

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import PendingRollbackError

load_dotenv()
st.set_page_config(page_title="Retail Decision Dashboard", layout="wide")

# KPI cards use Streamlit's native bordered container, with just enough
# spacing to distinguish each metric without turning the dashboard into a
# grid of heavy panels.
st.markdown(
    """
    <style>
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(250, 250, 252, 0.7);
        border-color: rgba(49, 51, 63, 0.14);
        border-radius: 0.6rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stMetric"] {
        padding: 0.15rem 0.25rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMetricLabel"] {
        margin-bottom: 0.15rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

page = st.sidebar.radio("View", ["Selected Period", "Full History"], index=1)


@st.cache_resource
def get_engine():
    db_url = os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL")
    if not db_url:
        st.error("DATABASE_URL not found in environment or Streamlit secrets.")
        st.stop()
    return create_engine(db_url, pool_pre_ping=True)


@st.cache_data(ttl=300)
def run_query(sql):
    engine = get_engine()
    try:
        with engine.connect() as conn:
            return pd.read_sql(sql, conn)
    except PendingRollbackError:
        with engine.connect() as conn:
            conn.rollback()
            return pd.read_sql(sql, conn)
    except Exception as exc:
        st.warning(f"Could not load dashboard data: {exc}")
        return pd.DataFrame()


def get_available_years():
    year_rows = run_query("""
        SELECT DISTINCT EXTRACT(YEAR FROM date_id) AS year
        FROM fact_sales
        WHERE date_id IS NOT NULL
        ORDER BY year
    """)
    if year_rows.empty:
        return [2009]
    return [int(year) for year in year_rows["year"].dropna().tolist()]


def get_year_filter_clause(selected_year):
    if selected_year is None:
        return "1=1"
    return f"EXTRACT(YEAR FROM date_id) = {selected_year}"


def get_alert_year_filter_clause(selected_year):
    if selected_year is None:
        return "1=1"
    return f"EXTRACT(YEAR FROM triggered_at) = {selected_year}"


def render_kpi_card(title, value, help_text="", delta=None, delta_description=None):
    """Render a compact, scannable KPI card with optional supporting detail."""
    with st.container(border=True):
        st.metric(
            title,
            value,
            delta=delta,
            help=help_text or None,
            delta_description=delta_description,
        )


def render_compact_metrics(metrics):
    """Show supporting measures without adding another layer of visual cards."""
    for row_start in range(0, len(metrics), 2):
        cols = st.columns(2, gap="medium")
        for idx, (title, value, help_text) in enumerate(metrics[row_start:row_start + 2]):
            with cols[idx]:
                st.metric(title, value, help=help_text or None)


def percentage_change(current, previous):
    if current is None or previous is None or pd.isna(current) or pd.isna(previous) or previous == 0:
        return None
    return f"{((current - previous) / abs(previous) * 100):+.1f}%"


def get_year_comparison(selected_year):
    if selected_year is None:
        return {}
    comparison = run_query(f"""
        SELECT
            SUM(revenue) FILTER (WHERE NOT is_return AND EXTRACT(YEAR FROM date_id) = {selected_year}) AS revenue,
            SUM(profit) FILTER (WHERE EXTRACT(YEAR FROM date_id) = {selected_year}) AS profit,
            COUNT(DISTINCT invoice_no) FILTER (WHERE NOT is_return AND EXTRACT(YEAR FROM date_id) = {selected_year}) AS orders,
            SUM(quantity) FILTER (WHERE EXTRACT(YEAR FROM date_id) = {selected_year}) AS units,
            SUM(revenue) FILTER (WHERE NOT is_return AND EXTRACT(YEAR FROM date_id) = {selected_year - 1}) AS prior_revenue,
            SUM(profit) FILTER (WHERE EXTRACT(YEAR FROM date_id) = {selected_year - 1}) AS prior_profit,
            COUNT(DISTINCT invoice_no) FILTER (WHERE NOT is_return AND EXTRACT(YEAR FROM date_id) = {selected_year - 1}) AS prior_orders,
            SUM(quantity) FILTER (WHERE EXTRACT(YEAR FROM date_id) = {selected_year - 1}) AS prior_units
        FROM fact_sales
    """)
    return comparison.iloc[0].to_dict() if not comparison.empty else {}


def get_customer_concentration(date_filter):
    concentration = run_query(f"""
        WITH customer_revenue AS (
            SELECT customer_id, SUM(revenue) AS revenue
            FROM fact_sales
            WHERE NOT is_return AND customer_id != 'GUEST' AND {date_filter}
            GROUP BY customer_id
        ), ranked AS (
            SELECT revenue, ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rank
            FROM customer_revenue
        )
        SELECT COALESCE(SUM(revenue) FILTER (WHERE rank <= 10)
            / NULLIF(SUM(revenue), 0) * 100, 0) AS top_ten_share
        FROM ranked
    """)
    return float(concentration.iloc[0]["top_ten_share"]) if not concentration.empty else 0.0


def get_data_freshness():
    freshness = run_query("""
        SELECT MAX(date_id) AS latest_transaction_date, MAX(loaded_at) AS latest_loaded_at
        FROM fact_sales
    """)
    return freshness.iloc[0] if not freshness.empty else None


def get_latest_forecast_summary():
    return run_query("""
        WITH latest_run AS (
            SELECT forecast_run_id, forecast_week, backtest_wape, sku_count
            FROM forecast_run
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
        )
        SELECT
            latest_run.forecast_week,
            latest_run.backtest_wape,
            latest_run.sku_count,
            SUM(fs.forecast_revenue) AS forecast_revenue,
            COUNT(*) FILTER (WHERE fs.risk_level = 'high') AS high_risk_skus
        FROM latest_run
        JOIN forecast_sales fs ON fs.forecast_run_id = latest_run.forecast_run_id
        GROUP BY latest_run.forecast_week, latest_run.backtest_wape, latest_run.sku_count
    """)


def get_latest_forecast_risks():
    return run_query("""
        WITH latest_run AS (
            SELECT forecast_run_id
            FROM forecast_run
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
        )
        SELECT
            fs.stock_code AS "Product code",
            p.description AS "Product",
            fs.forecast_revenue AS "Forecast revenue",
            fs.lower_revenue AS "Lower range",
            fs.upper_revenue AS "Upper range",
            fs.expected_change_pct AS "Expected change",
            fs.confidence_score * 100 AS "Confidence"
        FROM forecast_sales fs
        JOIN latest_run lr ON lr.forecast_run_id = fs.forecast_run_id
        JOIN dim_product p ON p.stock_code = fs.stock_code
        WHERE fs.risk_level = 'high'
        ORDER BY fs.expected_change_pct ASC, fs.confidence_score DESC
        LIMIT 10
    """)


def get_latest_forecast_chart():
    """Return recent actual revenue plus the next aggregated forecast range."""
    return run_query("""
        WITH latest_run AS (
            SELECT forecast_run_id, training_end_week, forecast_week
            FROM forecast_run
            WHERE status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
        ), actuals AS (
            SELECT
                date_trunc('week', f.date_id)::date AS week_start,
                SUM(f.revenue) AS actual_revenue,
                NULL::numeric AS forecast_revenue,
                NULL::numeric AS lower_forecast_range,
                NULL::numeric AS upper_forecast_range
            FROM fact_sales f
            CROSS JOIN latest_run lr
            WHERE NOT f.is_return
              AND f.date_id >= lr.training_end_week - INTERVAL '7 weeks'
              AND f.date_id < lr.forecast_week
            GROUP BY date_trunc('week', f.date_id)::date
        ), prediction AS (
            SELECT
                lr.forecast_week AS week_start,
                NULL::numeric AS actual_revenue,
                SUM(fs.forecast_revenue) AS forecast_revenue,
                SUM(fs.lower_revenue) AS lower_forecast_range,
                SUM(fs.upper_revenue) AS upper_forecast_range
            FROM latest_run lr
            JOIN forecast_sales fs ON fs.forecast_run_id = lr.forecast_run_id
            GROUP BY lr.forecast_week
        )
        SELECT * FROM actuals
        UNION ALL
        SELECT * FROM prediction
        ORDER BY week_start
    """)


def render_demand_outlook():
    """Display the latest persisted forecast without retraining during a UI rerun."""
    summary = get_latest_forecast_summary()
    st.subheader("Next-week demand outlook")
    if summary.empty:
        st.info("No forecast is available yet. Run `python src/forecast.py` after the ETL completes.")
        return

    forecast = summary.iloc[0]
    forecast_week = pd.Timestamp(forecast["forecast_week"])
    cols = st.columns(2, gap="medium")
    with cols[0]:
        render_kpi_card(
            "Predicted revenue",
            f"£{forecast['forecast_revenue']:,.0f}",
            f"Expected completed sales for the week beginning {forecast_week:%d %b %Y}",
        )
    with cols[1]:
        render_kpi_card(
            "Forecast backtest error",
            f"{forecast['backtest_wape']:.1f}%",
            "WAPE on the latest completed week; lower is better.",
        )

    st.caption(
        f"{int(forecast['sku_count']):,} active products forecast • "
        f"{int(forecast['high_risk_skus']):,} high-confidence demand risks"
    )
    chart_data = get_latest_forecast_chart()
    if not chart_data.empty:
        st.markdown("**Recent revenue and next-week forecast**")
        st.line_chart(
            chart_data.rename(columns={
                "actual_revenue": "Actual revenue",
                "forecast_revenue": "Forecast revenue",
                "lower_forecast_range": "Lower forecast range",
                "upper_forecast_range": "Upper forecast range",
            }).set_index("week_start"),
            height=300,
        )
        st.caption("The forecast range represents uncertainty; actual revenue will appear after the next data load.")

    risks = get_latest_forecast_risks()
    if risks.empty:
        st.success("No high-confidence demand declines are predicted for the next week.")
    else:
        st.caption("Products expected to decline by at least 20%, where the model has sufficient consistency.")
        st.dataframe(
            risks,
            width="stretch",
            hide_index=True,
            column_config={
                "Forecast revenue": st.column_config.NumberColumn(format="£%.0f"),
                "Lower range": st.column_config.NumberColumn(format="£%.0f"),
                "Upper range": st.column_config.NumberColumn(format="£%.0f"),
                "Expected change": st.column_config.NumberColumn(format="%.1f%%"),
                "Confidence": st.column_config.NumberColumn(format="%.0f%%"),
            },
        )


st.title("Retail Decision Dashboard")
st.caption("Live view of the warehouse data — refreshes automatically after each pipeline run.")

freshness = get_data_freshness()
if freshness is not None and pd.notna(freshness["latest_transaction_date"]):
    st.caption(
        f"Latest transaction: {freshness['latest_transaction_date']:%d %b %Y} "
        f"• Warehouse loaded: {freshness['latest_loaded_at']:%d %b %Y %H:%M}"
    )

if page == "Selected Period":
    st.sidebar.header("Year filter")
    available_years = get_available_years()
    year_options = ["All years"] + [str(year) for year in available_years]
    selected_year_label = st.sidebar.selectbox("Year", year_options, index=0)
    selected_year = None if selected_year_label == "All years" else int(selected_year_label)
    st.sidebar.caption("Choose a single year or view all years together.")

    date_filter = get_year_filter_clause(selected_year)
    year_label = "all years" if selected_year is None else str(selected_year)
    st.caption(f"Showing data for {year_label}.")

    kpi_frame = run_query(f"""
        SELECT
            SUM(revenue) FILTER (WHERE NOT is_return) AS gross_revenue,
            SUM(revenue) AS net_revenue,
            SUM(profit) AS net_profit,
            COUNT(DISTINCT invoice_no) FILTER (WHERE NOT is_return) AS total_orders,
            COUNT(DISTINCT customer_id) FILTER (WHERE customer_id != 'GUEST') AS registered_customers,
            SUM(quantity) AS net_units,
            COUNT(DISTINCT stock_code) FILTER (WHERE NOT is_return) AS active_skus,
            COALESCE(ABS(SUM(revenue) FILTER (WHERE is_return)), 0) AS return_value
        FROM fact_sales
        WHERE {date_filter}
    """)
    if kpi_frame.empty:
        st.warning("No data is available for the selected period yet.")
        st.stop()

    kpis = kpi_frame.iloc[0]
    margin_pct = (kpis["net_profit"] / kpis["net_revenue"] * 100) if kpis["net_revenue"] else 0
    aov = (kpis["net_revenue"] / kpis["total_orders"]) if kpis["total_orders"] else 0
    return_value_rate = (kpis["return_value"] / kpis["gross_revenue"] * 100) if kpis["gross_revenue"] else 0
    comparison = get_year_comparison(selected_year)
    concentration = get_customer_concentration(date_filter)

    primary_metrics = [
        ("Net revenue", f"£{kpis['net_revenue']:,.0f}", "Sales less return values", percentage_change(comparison.get("revenue"), comparison.get("prior_revenue"))),
        ("Estimated net profit", f"£{kpis['net_profit']:,.0f}", "Uses simulated SKU costs", percentage_change(comparison.get("profit"), comparison.get("prior_profit"))),
        ("Completed orders", f"{kpis['total_orders']:,}", "Excludes cancelled invoices", percentage_change(comparison.get("orders"), comparison.get("prior_orders"))),
        ("Net units", f"{kpis['net_units']:,.0f}", "Sold units less returned units", percentage_change(comparison.get("units"), comparison.get("prior_units"))),
    ]

    st.subheader("Selected-period snapshot")
    for row_start in range(0, len(primary_metrics), 2):
        cols = st.columns(2, gap="medium")
        for idx, (title, value, subtitle, delta) in enumerate(primary_metrics[row_start:row_start + 2]):
            with cols[idx]:
                render_kpi_card(title, value, subtitle, delta, "vs. prior year")

    secondary_metrics = [
        ("Estimated net margin", f"{margin_pct:.1f}%", "Estimated profit as a share of net revenue"),
        ("Net order value", f"£{aov:,.2f}", "Net revenue per completed order"),
        ("Return value rate", f"{return_value_rate:.1f}%", "Returned value as a share of gross sales"),
        ("Active products", f"{kpis['active_skus']:,}", "SKUs with at least one non-return sale"),
    ]

    st.subheader("Commercial health")
    render_compact_metrics(secondary_metrics)

    st.caption(
        f"Gross sales before returns: £{kpis['gross_revenue']:,.0f} • "
        f"Top 10 registered customers account for {concentration:.1f}% of registered-customer revenue."
    )

    st.subheader("Revenue Over Time")
    trend = run_query(f"""
        SELECT date_id AS date, SUM(revenue) AS revenue
        FROM fact_sales
        WHERE NOT is_return AND {date_filter}
        GROUP BY date_id
        ORDER BY date_id
    """)
    if not trend.empty:
        st.line_chart(trend.set_index("date"))

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Top 10 Products by Profit")
        top_products = run_query(f"""
            SELECT p.description, SUM(f.profit) AS profit
            FROM fact_sales f
            JOIN dim_product p ON p.stock_code = f.stock_code
            WHERE NOT f.is_return AND {date_filter}
            GROUP BY p.description
            ORDER BY profit DESC
            LIMIT 10
        """)
        if not top_products.empty:
            st.bar_chart(top_products.set_index("description"))

    with col_b:
        st.subheader("Top 10 Customers by Revenue")
        top_customers = run_query(f"""
            SELECT customer_id, SUM(revenue) AS revenue
            FROM fact_sales
            WHERE NOT is_return AND customer_id != 'GUEST' AND {date_filter}
            GROUP BY customer_id
            ORDER BY revenue DESC
            LIMIT 10
        """)
        if not top_customers.empty:
            st.bar_chart(top_customers.set_index("customer_id"))

    st.subheader("Recent Automated Alerts")
    st.caption(
        "Margin and forecast checks run automatically after each scheduled ETL job. "
        "Keeping delivery outside the dashboard prevents a viewer from triggering Slack alerts."
    )

    alerts = run_query(f"""
        SELECT triggered_at, stock_code, message
        FROM alert_log
        WHERE {get_alert_year_filter_clause(selected_year)}
        ORDER BY triggered_at DESC
        LIMIT 10
    """)
    if alerts.empty:
        st.info("No alerts have fired yet.")
    else:
        st.dataframe(alerts, width="stretch")

else:
    st.subheader("Full History Summary")
    all_time_kpi_frame = run_query("""
        SELECT
            SUM(revenue) FILTER (WHERE NOT is_return) AS gross_revenue,
            SUM(revenue) AS net_revenue,
            SUM(profit) AS net_profit,
            COUNT(DISTINCT invoice_no) FILTER (WHERE NOT is_return) AS total_orders,
            COUNT(DISTINCT customer_id) FILTER (WHERE customer_id != 'GUEST') AS registered_customers,
            SUM(quantity) AS net_units,
            COUNT(DISTINCT stock_code) FILTER (WHERE NOT is_return) AS active_skus,
            COALESCE(ABS(SUM(revenue) FILTER (WHERE is_return)), 0) AS return_value
        FROM fact_sales
    """)
    if all_time_kpi_frame.empty:
        st.warning("No full-history data is available yet.")
        st.stop()

    all_time_kpis = all_time_kpi_frame.iloc[0]
    all_time_margin_pct = (all_time_kpis["net_profit"] / all_time_kpis["net_revenue"] * 100) if all_time_kpis["net_revenue"] else 0
    all_time_aov = (all_time_kpis["net_revenue"] / all_time_kpis["total_orders"]) if all_time_kpis["total_orders"] else 0
    all_time_return_value_rate = (all_time_kpis["return_value"] / all_time_kpis["gross_revenue"] * 100) if all_time_kpis["gross_revenue"] else 0
    all_time_concentration = get_customer_concentration("1=1")

    all_time_customer_segmentation = run_query("""
        WITH customer_order_counts AS (
            SELECT customer_id, COUNT(DISTINCT invoice_no) AS order_count
            FROM fact_sales
            WHERE NOT is_return
            GROUP BY customer_id
        )
        SELECT
            COUNT(*) FILTER (WHERE customer_id != 'GUEST') AS registered_customers,
            COUNT(*) FILTER (WHERE customer_id != 'GUEST' AND order_count = 1) AS one_time_customers,
            COUNT(*) FILTER (WHERE customer_id != 'GUEST' AND order_count > 1) AS repeat_customers
        FROM customer_order_counts
    """).iloc[0]

    all_time_revenue_per_customer = (all_time_kpis["net_revenue"] / all_time_kpis["registered_customers"]) if all_time_kpis["registered_customers"] else 0

    all_time_primary_metrics = [
        ("Net revenue", f"£{all_time_kpis['net_revenue']:,.0f}", "Sales less return values"),
        ("Estimated net profit", f"£{all_time_kpis['net_profit']:,.0f}", "Uses simulated SKU costs"),
        ("Completed orders", f"{all_time_kpis['total_orders']:,}", "Excludes cancelled invoices"),
        ("Net units", f"{all_time_kpis['net_units']:,.0f}", "Sold units less returned units"),
    ]

    st.subheader("Business snapshot")
    for row_start in range(0, len(all_time_primary_metrics), 2):
        cols = st.columns(2, gap="medium")
        for idx, (title, value, subtitle) in enumerate(all_time_primary_metrics[row_start:row_start + 2]):
            with cols[idx]:
                render_kpi_card(title, value, subtitle)

    all_time_customer_metrics = [
        ("Registered customers", f"{all_time_customer_segmentation['registered_customers']:,}", "Guest checkout is excluded"),
        ("One-time customers", f"{all_time_customer_segmentation['one_time_customers']:,}", "Registered customers with one order"),
        ("Repeat Customers", f"{all_time_customer_segmentation['repeat_customers']:,}", "Customers with more than one order"),
        ("Net revenue / customer", f"£{all_time_revenue_per_customer:,.2f}", "Registered customers only"),
    ]

    st.subheader("Customer growth & retention")
    render_compact_metrics(all_time_customer_metrics)

    all_time_secondary_metrics = [
        ("Estimated net margin", f"{all_time_margin_pct:.1f}%", "Estimated profit as a share of net revenue"),
        ("Net order value", f"£{all_time_aov:,.2f}", "Net revenue per completed order"),
        ("Return value rate", f"{all_time_return_value_rate:.1f}%", "Returned value as a share of gross sales"),
        ("Active products", f"{all_time_kpis['active_skus']:,}", "SKUs with at least one non-return sale"),
    ]

    st.subheader("Commercial health")
    render_compact_metrics(all_time_secondary_metrics)

    st.caption(
        f"Gross sales before returns: £{all_time_kpis['gross_revenue']:,.0f} • "
        f"Top 10 registered customers account for {all_time_concentration:.1f}% of registered-customer revenue."
    )
    render_demand_outlook()
    st.caption("This page includes every year present in the warehouse.")
    st.caption("Use the Selected Period page for date-specific charts and alerts.")
