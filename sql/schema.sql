-- Star schema for the Retail Decision Automation Platform
-- Run this once against your Neon Postgres database before the first ETL run.
-- Safe to re-run: every statement uses IF NOT EXISTS / DROP ... CASCADE.

DROP TABLE IF EXISTS fact_sales CASCADE;
DROP TABLE IF EXISTS dim_product CASCADE;
DROP TABLE IF EXISTS dim_customer CASCADE;
DROP TABLE IF EXISTS dim_date CASCADE;
DROP TABLE IF EXISTS dim_location CASCADE;
DROP TABLE IF EXISTS alert_log CASCADE;

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
    loaded_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_fact_sales_date ON fact_sales(date_id);
CREATE INDEX idx_fact_sales_product ON fact_sales(stock_code);
CREATE INDEX idx_fact_sales_customer ON fact_sales(customer_id);

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
    metric_value NUMERIC(12, 2)
);
