"""Create an explainable weekly demand forecast for each active product.

The model is deliberately simple: a recency-weighted four-week revenue
baseline. It is easy to audit, has no extra ML dependency, and is backtested
against the latest completed week before predictions are published.
"""

import math

import pandas as pd
from psycopg2.extras import execute_values

from db import get_connection


MODEL_NAME = "recency_weighted_4_week_baseline"
MIN_HISTORY_WEEKS = 8
RISK_DECLINE_PCT = -20.0
HIGH_CONFIDENCE = 0.60


def get_weekly_revenue(conn):
    """Return completed-week revenue only, so partial weeks never train the model."""
    query = """
        WITH latest AS (
            SELECT date_trunc('week', MAX(date_id))::date AS current_week
            FROM fact_sales
        )
        SELECT
            f.stock_code,
            p.description,
            latest.current_week,
            date_trunc('week', f.date_id)::date AS week_start,
            SUM(f.revenue)::float AS revenue
        FROM fact_sales f
        JOIN dim_product p ON p.stock_code = f.stock_code
        CROSS JOIN latest
        WHERE NOT f.is_return
          AND f.date_id < latest.current_week
        GROUP BY f.stock_code, p.description, latest.current_week,
                 date_trunc('week', f.date_id)::date
        ORDER BY f.stock_code, week_start
    """
    with conn.cursor() as cur:
        cur.execute(query)
        columns = [column.name for column in cur.description]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns)


def weighted_forecast(values):
    """Forecast from the most recent four weekly observations."""
    recent = values[-4:]
    weights = [1, 2, 3, 4]
    return sum(value * weight for value, weight in zip(recent, weights)) / sum(weights)


def build_forecasts(weekly_revenue):
    """Build SKU forecasts and a final-week, time-based backtest WAPE."""
    if weekly_revenue.empty:
        return pd.DataFrame(), None, None, None

    all_weeks = pd.date_range(
        weekly_revenue["week_start"].min(),
        weekly_revenue["week_start"].max(),
        freq="W-MON",
    )
    latest_week = all_weeks[-1]
    # The source can end mid-week. Keep that partial week out of training and
    # forecast the following full week, rather than labelling a forecast as a
    # week that has already begun.
    current_week = (
        pd.Timestamp(weekly_revenue["current_week"].iloc[0])
        if "current_week" in weekly_revenue.columns
        else latest_week + pd.Timedelta(days=7)
    )
    forecast_week = current_week + pd.Timedelta(days=7)
    results = []
    total_absolute_error = 0.0
    total_actual = 0.0

    for (stock_code, description), group in weekly_revenue.groupby(["stock_code", "description"]):
        series = group.set_index("week_start")["revenue"].reindex(all_weeks, fill_value=0.0)
        values = series.astype(float).tolist()
        if len(values) < MIN_HISTORY_WEEKS or sum(values[-4:]) <= 0:
            continue

        # Time-based validation: use all weeks before the final completed one
        # to predict that final week. Never shuffle observations.
        validation_prediction = weighted_forecast(values[:-1])
        validation_actual = values[-1]
        total_absolute_error += abs(validation_actual - validation_prediction)
        total_actual += abs(validation_actual)

        prediction = weighted_forecast(values)
        prior_four_week_avg = sum(values[-4:]) / 4
        expected_change = (
            (prediction - prior_four_week_avg) / prior_four_week_avg * 100
            if prior_four_week_avg else 0.0
        )

        residuals = [
            values[index] - weighted_forecast(values[:index])
            for index in range(4, len(values))
        ]
        residual_std = pd.Series(residuals).std(ddof=1) if len(residuals) > 1 else 0.0
        residual_std = 0.0 if pd.isna(residual_std) else float(residual_std)
        interval = 1.96 * residual_std
        coefficient_of_variation = residual_std / max(prediction, 1.0)
        confidence = max(0.0, min(1.0, 1 - coefficient_of_variation))
        risk_level = (
            "high"
            if expected_change <= RISK_DECLINE_PCT and confidence >= HIGH_CONFIDENCE
            else "normal"
        )
        results.append({
            "stock_code": stock_code,
            "forecast_week": forecast_week.date(),
            "forecast_revenue": round(prediction, 2),
            "lower_revenue": round(max(0.0, prediction - interval), 2),
            "upper_revenue": round(prediction + interval, 2),
            "prior_four_week_avg": round(prior_four_week_avg, 2),
            "expected_change_pct": round(expected_change, 2),
            "risk_level": risk_level,
            "confidence_score": round(confidence, 4),
        })

    wape = total_absolute_error / total_actual * 100 if total_actual else None
    return pd.DataFrame(results), latest_week.date(), forecast_week.date(), wape


def ensure_forecast_tables(conn):
    """Support existing demo databases without requiring a destructive schema reset."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forecast_run (
                forecast_run_id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                model_name VARCHAR(100) NOT NULL,
                training_end_week DATE NOT NULL,
                forecast_week DATE NOT NULL,
                backtest_wape NUMERIC(8, 4),
                sku_count INT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'completed'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forecast_sales (
                forecast_run_id BIGINT NOT NULL REFERENCES forecast_run(forecast_run_id) ON DELETE CASCADE,
                stock_code VARCHAR(20) NOT NULL REFERENCES dim_product(stock_code),
                forecast_week DATE NOT NULL,
                forecast_revenue NUMERIC(12, 2) NOT NULL,
                lower_revenue NUMERIC(12, 2) NOT NULL,
                upper_revenue NUMERIC(12, 2) NOT NULL,
                prior_four_week_avg NUMERIC(12, 2) NOT NULL,
                expected_change_pct NUMERIC(8, 2),
                risk_level VARCHAR(20) NOT NULL DEFAULT 'normal',
                confidence_score NUMERIC(5, 4) NOT NULL,
                PRIMARY KEY (forecast_run_id, stock_code)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_forecast_sales_week ON forecast_sales(forecast_week)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_forecast_sales_risk ON forecast_sales(risk_level)")
    conn.commit()


def save_forecasts(conn, forecasts, training_end_week, forecast_week, wape):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO forecast_run
               (model_name, training_end_week, forecast_week, backtest_wape, sku_count)
               VALUES (%s, %s, %s, %s, %s) RETURNING forecast_run_id""",
            (MODEL_NAME, training_end_week, forecast_week, wape, len(forecasts)),
        )
        forecast_run_id = cur.fetchone()[0]
        rows = [
            (forecast_run_id, *row)
            for row in forecasts[[
                "stock_code", "forecast_week", "forecast_revenue", "lower_revenue",
                "upper_revenue", "prior_four_week_avg", "expected_change_pct",
                "risk_level", "confidence_score",
            ]].itertuples(index=False, name=None)
        ]
        execute_values(
            cur,
            """INSERT INTO forecast_sales
               (forecast_run_id, stock_code, forecast_week, forecast_revenue, lower_revenue,
                upper_revenue, prior_four_week_avg, expected_change_pct, risk_level,
                confidence_score)
               VALUES %s""",
            rows,
        )
    conn.commit()
    return forecast_run_id


def main():
    conn = get_connection()
    try:
        ensure_forecast_tables(conn)
        forecasts, training_end_week, forecast_week, wape = build_forecasts(get_weekly_revenue(conn))
        if forecasts.empty:
            print("Not enough completed weekly history to create a forecast.")
            return
        run_id = save_forecasts(conn, forecasts, training_end_week, forecast_week, wape)
        risk_count = int((forecasts["risk_level"] == "high").sum())
        wape_display = f"{wape:.1f}%" if wape is not None and not math.isnan(wape) else "n/a"
        print(
            f"Saved forecast run {run_id}: {len(forecasts)} SKUs for week beginning "
            f"{forecast_week}; backtest WAPE {wape_display}; {risk_count} high-risk SKUs."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
