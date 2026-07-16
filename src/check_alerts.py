"""
check_alerts.py — the "automated decision" layer of the project.

Queries the warehouse for a defined condition (a product's profit margin
dropping sharply week over week), and if triggered, posts a message to
Slack — optionally using the Anthropic API to turn the raw numbers into a
short written recommendation instead of a bare number.

IMPORTANT — historical dataset note:
The Online Retail II dataset covers 2009-2011, it isn't live data. There is
no real "today" to compare "this week" against. So this script treats the
most recent date present in fact_sales as if it were "today", and compares
the 7 days before that to the 7 days before that. This is a deliberate,
disclosed adaptation so the automation logic can be demonstrated against a
static historical dataset — a genuinely live feed would just use NOW().
"""

import os
import requests
from db import get_connection

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None

MARGIN_DROP_THRESHOLD_PCT = 10.0  # flag if margin fell more than this, week over week


def _get_secret(name):
    value = os.environ.get(name)
    if value:
        return value
    if st is not None:
        try:
            return st.secrets.get(name)
        except Exception:
            return None
    return None


SLACK_WEBHOOK_URL = _get_secret("SLACK_WEBHOOK_URL")
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY")  # optional


MARGIN_DROP_QUERY = """
WITH ref_date AS (
    SELECT MAX(date_id) AS max_date FROM fact_sales
),
recent_week AS (
    SELECT stock_code,
           SUM(profit) AS profit,
           SUM(revenue) AS revenue
    FROM fact_sales, ref_date
    WHERE date_id > ref_date.max_date - INTERVAL '7 days'
      AND date_id <= ref_date.max_date
      AND NOT is_return
    GROUP BY stock_code
),
prior_week AS (
    SELECT stock_code,
           SUM(profit) AS profit,
           SUM(revenue) AS revenue
    FROM fact_sales, ref_date
    WHERE date_id > ref_date.max_date - INTERVAL '14 days'
      AND date_id <= ref_date.max_date - INTERVAL '7 days'
      AND NOT is_return
    GROUP BY stock_code
)
SELECT
    r.stock_code,
    p.description,
    ref_date.max_date AS period_end,
    prior.revenue  AS prior_revenue,
    recent.revenue AS recent_revenue,
    CASE WHEN prior.revenue > 0 THEN prior.profit / prior.revenue * 100 ELSE NULL END AS prior_margin_pct,
    CASE WHEN recent.revenue > 0 THEN recent.profit / recent.revenue * 100 ELSE NULL END AS recent_margin_pct
FROM recent_week recent
JOIN prior_week prior ON recent.stock_code = prior.stock_code
JOIN dim_product p ON p.stock_code = recent.stock_code
JOIN recent_week r ON r.stock_code = recent.stock_code
CROSS JOIN ref_date
WHERE prior.revenue > 50  -- ignore near-zero-volume products, too noisy
  AND prior.profit / NULLIF(prior.revenue, 0) * 100
      - recent.profit / NULLIF(recent.revenue, 0) * 100 > %s
ORDER BY (prior.profit / NULLIF(prior.revenue, 0) - recent.profit / NULLIF(recent.revenue, 0)) DESC
LIMIT 5;
"""


def find_margin_drops(conn):
    with conn.cursor() as cur:
        cur.execute(MARGIN_DROP_QUERY, (MARGIN_DROP_THRESHOLD_PCT,))
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows


def draft_recommendation(row):
    """
    Turns a flagged row into a short written recommendation.
    Uses the Anthropic API if a key is configured; otherwise falls back
    to a plain templated sentence so the script still works without it.
    """
    prior = row["prior_margin_pct"]
    recent = row["recent_margin_pct"]
    desc = row["description"]
    code = row["stock_code"]

    fallback = (
        f"Product {code} ({desc}) margin dropped from {prior:.1f}% to "
        f"{recent:.1f}% week over week. Consider reviewing pricing, "
        f"discounting, or supplier cost for this item."
    )

    if not ANTHROPIC_API_KEY:
        return fallback

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 150,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"A retail product's profit margin dropped from "
                        f"{prior:.1f}% to {recent:.1f}% in the last week. "
                        f"Product: {desc} (code {code}). "
                        f"In 2-3 sentences, write a plain-language flag for a "
                        f"retail manager explaining the issue and one concrete "
                        f"next step to investigate. No preamble."
                    ),
                }],
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"Anthropic API call failed, using fallback message: {e}")
        return fallback


def send_to_slack(message):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set — printing alert instead of sending:")
        print(message)
        return
    response = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)
    response.raise_for_status()


def log_alert(conn, row, message):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO alert_log
               (alert_type, stock_code, message, metric_value, period_end)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (alert_type, stock_code, period_end) DO NOTHING
               RETURNING alert_id""",
            ("margin_drop", row["stock_code"], message, row["recent_margin_pct"], row["period_end"]),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


def main():
    conn = get_connection()
    flagged = find_margin_drops(conn)

    if not flagged:
        print("No margin drops above threshold this run.")
        conn.close()
        return

    print(f"{len(flagged)} product(s) flagged.")
    for row in flagged:
        message = draft_recommendation(row)
        if not log_alert(conn, row, message):
            print(f"Alert for {row['stock_code']} already logged for this period.")
            continue
        send_to_slack(f":rotating_light: *Margin alert*\n{message}")
        print(f"Alerted on {row['stock_code']}: {message}")

    conn.close()


if __name__ == "__main__":
    main()
