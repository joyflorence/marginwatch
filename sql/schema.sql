-- Star schema for the Retail Decision Automation Platform
-- Run this once against your Neon Postgres database before the first ETL run.
-- Safe to re-run: every statement uses IF NOT EXISTS / DROP ... CASCADE.

DROP TABLE IF EXISTS fact_sales CASCADE;
DROP TABLE IF EXISTS forecast_sales CASCADE;
DROP TABLE IF EXISTS forecast_run CASCADE;
DROP TABLE IF EXISTS dim_product CASCADE;
DROP TABLE IF EXISTS dim_customer CASCADE;
DROP TABLE IF EXISTS dim_date CASCADE;
DROP TABLE IF EXISTS dim_location CASCADE;
DROP TABLE IF EXISTS alert_log CASCADE;
DROP TABLE IF EXISTS etl_quality_check CASCADE;
DROP TABLE IF EXISTS etl_run_log CASCADE;
DROP TABLE IF EXISTS etl_control CASCADE;

-- ---------------------------------------------------------
-- Dimension: Product
-- ---------------------------------------------------------
CREATE TABLE dim_product (
    stock_code      VARCHAR(20) PRIMARY KEY,
    description     TEXT,
    unit_cost       NUMERIC(10, 2),   -- simulated, see README for why
    first_seen_date DATE
);

-- ---------------------------------------------------------
-- Dimension: Customer
-- ---------------------------------------------------------
CREATE TABLE dim_customer (
    customer_id     VARCHAR(20) PRIMARY KEY,   -- 'GUEST' for null customer IDs
    country         VARCHAR(100),
    first_order_date DATE
);

-- ---------------------------------------------------------
-- Dimension: Date
-- ---------------------------------------------------------
CREATE TABLE dim_date (
    date_id     DATE PRIMARY KEY,
    year        INT,
    quarter     INT,
    month       INT,
    month_name  VARCHAR(20),
    week        INT,
    day_of_week VARCHAR(20),
    is_weekend  BOOLEAN
);

-- ---------------------------------------------------------
-- Dimension: Location
-- ---------------------------------------------------------
CREATE TABLE dim_location (
    location_id SERIAL PRIMARY KEY,
    country     VARCHAR(100) UNIQUE NOT NULL
);

-- ---------------------------------------------------------
-- Fact: Sales
-- One row per invoice line item.
-- ---------------------------------------------------------
CREATE TABLE fact_sales (
    sales_id        BIGSERIAL PRIMARY KEY,
    invoice_no      VARCHAR(20) NOT NULL,
    stock_code      VARCHAR(20) REFERENCES dim_product(stock_code),
    customer_id     VARCHAR(20) REFERENCES dim_customer(customer_id),
    date_id         DATE REFERENCES dim_date(date_id),
    location_id     INT REFERENCES dim_location(location_id),
    quantity        INT NOT NULL,
    unit_price      NUMERIC(10, 2) NOT NULL,
    unit_cost       NUMERIC(10, 2),
    revenue         NUMERIC(12, 2) NOT NULL,
    profit          NUMERIC(12, 2),
    is_return       BOOLEAN DEFAULT FALSE,
    source_row_key  VARCHAR(64) NOT NULL UNIQUE,
    loaded_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_fact_sales_date ON fact_sales(date_id);
CREATE INDEX idx_fact_sales_product ON fact_sales(stock_code);
CREATE INDEX idx_fact_sales_customer ON fact_sales(customer_id);

-- ---------------------------------------------------------
-- ETL control: records the most recent source date successfully loaded.
-- Historical rows stay in fact_sales; later ETL runs only append rows
-- from newer source dates (with a unique source key as a second safeguard).
-- ---------------------------------------------------------
CREATE TABLE etl_control (
    id               INT PRIMARY KEY DEFAULT 1,
    last_loaded_date DATE,           -- NULL means nothing loaded yet
    CONSTRAINT single_row CHECK (id = 1)
);
INSERT INTO etl_control (id, last_loaded_date) VALUES (1, NULL);

-- ---------------------------------------------------------
-- ETL audit: one record per pipeline execution, plus its
-- data-quality checks. These tables make each load observable
-- and provide a durable explanation for a failed run.
-- ---------------------------------------------------------
CREATE TABLE etl_run_log (
    run_id              BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMP,
    status              VARCHAR(20) NOT NULL,
    source_path         TEXT NOT NULL,
    source_rows         INT,
    clean_rows          INT,
    candidate_fact_rows INT,
    inserted_fact_rows  INT,
    rejected_rows       INT,
    last_source_date    DATE,
    error_message       TEXT
);

CREATE TABLE etl_quality_check (
    quality_check_id BIGSERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES etl_run_log(run_id) ON DELETE CASCADE,
    check_name       VARCHAR(100) NOT NULL,
    status           VARCHAR(20) NOT NULL,
    observed_value   INT,
    details          TEXT,
    checked_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_etl_run_log_started_at ON etl_run_log(started_at DESC);
CREATE INDEX idx_etl_quality_check_run_id ON etl_quality_check(run_id);

-- ---------------------------------------------------------
-- Alert log: every alert the automation layer has ever fired
-- This table is what makes the automation demonstrable —
-- it's evidence the check actually ran, not just a claim.
-- ---------------------------------------------------------
CREATE TABLE alert_log (
    alert_id     BIGSERIAL PRIMARY KEY,
    triggered_at TIMESTAMP DEFAULT NOW(),
    alert_type   VARCHAR(50),
    stock_code   VARCHAR(20),
    message      TEXT,
    metric_value NUMERIC(12, 2),
    period_end   DATE NOT NULL,
    CONSTRAINT unique_margin_alert_per_period
        UNIQUE (alert_type, stock_code, period_end)
);

-- ---------------------------------------------------------
-- Forecasts: a versioned, explainable weekly demand forecast per SKU.
-- Each run is retained so dashboard users can compare forecast runs.
-- ---------------------------------------------------------
CREATE TABLE forecast_run (
    forecast_run_id   BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMP NOT NULL DEFAULT NOW(),
    model_name        VARCHAR(100) NOT NULL,
    training_end_week DATE NOT NULL,
    forecast_week     DATE NOT NULL,
    backtest_wape     NUMERIC(8, 4),
    sku_count         INT NOT NULL,
    status            VARCHAR(20) NOT NULL DEFAULT 'completed'
);

CREATE TABLE forecast_sales (
    forecast_run_id       BIGINT NOT NULL REFERENCES forecast_run(forecast_run_id) ON DELETE CASCADE,
    stock_code            VARCHAR(20) NOT NULL REFERENCES dim_product(stock_code),
    forecast_week         DATE NOT NULL,
    forecast_revenue      NUMERIC(12, 2) NOT NULL,
    lower_revenue         NUMERIC(12, 2) NOT NULL,
    upper_revenue         NUMERIC(12, 2) NOT NULL,
    prior_four_week_avg   NUMERIC(12, 2) NOT NULL,
    expected_change_pct   NUMERIC(8, 2),
    risk_level            VARCHAR(20) NOT NULL DEFAULT 'normal',
    confidence_score      NUMERIC(5, 4) NOT NULL,
    PRIMARY KEY (forecast_run_id, stock_code)
);

CREATE INDEX idx_forecast_sales_week ON forecast_sales(forecast_week);
CREATE INDEX idx_forecast_sales_risk ON forecast_sales(risk_level);
