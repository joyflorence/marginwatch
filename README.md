# Retail Decision Automation Platform

An automated pipeline that loads the Online Retail II (UCI) dataset into a
Postgres star schema, keeps a live dashboard in sync with it, and
automatically flags products with a shrinking profit margin — posting a
written recommendation to Slack without anyone checking a report.

This README covers **every step**, including the parts that aren't code:
setting up Neon, wiring up GitHub secrets, configuring Grafana Cloud, and
creating a Slack webhook. Follow it top to bottom for a first-time setup.

---

## Project structure

```
retail-pipeline/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── data/                        <- you create this, see Step 1
│   └── online_retail_ii.xlsx
├── sql/
│   └── schema.sql               <- run once to create the star schema
├── src/
│   ├── db.py                    <- shared Postgres connection helper
│   ├── setup_db.py              <- one-time schema creation script
│   ├── etl.py                   <- cleans + loads data (runs every schedule)
│   └── check_alerts.py          <- automated margin-drop alert (runs every schedule)
└── .github/
    └── workflows/
        └── pipeline.yml         <- GitHub Actions automation
```

---

## Step 1 — Get the dataset out of Kaggle

Dataset is available at
`/kaggle/input/datasets/cgrymn/online-retail-ii-uci-dataset` — that path
only exists **inside a Kaggle notebook**. GitHub Actions runs on GitHub's
own servers, which have no access to Kaggle's filesystem, so the dataset
needs to physically live in your repo.

**Process (no code required):**
1. Open a new Kaggle notebook and add the dataset
   `cgrymn/online-retail-ii-uci-dataset` as a data source (the "+ Add Input"
   button).
2. In a notebook cell, run:
   ```python
   import shutil, glob
   src = glob.glob("/kaggle/input/datasets/cgrymn/online-retail-ii-uci-dataset/*")[0]
   shutil.copy(src, "/kaggle/working/online_retail_ii.csv")
   ```
   (If the source file is `.xlsx` instead of `.csv`, keep the original
   extension — `etl.py` handles both.)
3. In the Kaggle notebook's output pane, download the copied file.
4. In your project folder on your own machine, create a `data/` folder and
   place the downloaded file inside it as `data/online_retail_ii.csv`
   (or `.xlsx`).
5. Commit that file to your GitHub repo. It's a static historical dataset,
   so committing it is the simplest option — no repeated downloads, and
   GitHub Actions can read it directly.

> If the file is larger than GitHub's 100MB per-file limit, use
> [Git LFS](https://git-lfs.com/) to track it — `git lfs track "data/*.csv"`
> before committing.

---

## Step 2 — Create your Neon database

**Process (no code required):**
1. Go to [neon.tech](https://neon.tech) and sign up (free, no credit card).
2. Create a new project — any name, e.g. `retail-pipeline`.
3. On the project dashboard, find the **connection string** (it looks like
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`).
   Copy it — you'll need it twice: once locally, once as a GitHub secret.

---

## Step 3 — Set up your local environment

```bash
git clone <your-repo-url>
cd retail-pipeline
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and paste in your real Neon connection string and (for now)
leave `SLACK_WEBHOOK_URL` and `ANTHROPIC_API_KEY` blank — you'll fill
those in during Steps 6 and 7.

Since `.env` isn't loaded automatically by plain Python, load it before
running any script:

```bash
export $(grep -v '^#' .env | xargs)      # Mac/Linux
```
(On Windows, use a tool like `python-dotenv`'s CLI, or set the variables
manually in your shell.)

---

## Step 4 — Create the star schema

Run this once:

```bash
python src/setup_db.py
```

You should see `Schema created successfully.` If you get a connection
error, double check the connection string was copied in full, including
`?sslmode=require` at the end.

---

## Step 5 — Run the pipeline locally (first test)

```bash
python src/etl.py
```

This reads `data/online_retail_ii.xlsx`, cleans it, and loads every
available historical year into Neon on the first run.
You should see progress lines for each table, ending in
`ETL run complete.`

**A note on profit figures:** this dataset only has sale price, not cost —
so there is no real profit number to compute. `etl.py` generates a
simulated unit cost (a random 40–70% of sale price) specifically so the
margin/profitability analysis in this project has something real to work
with. This is disclosed here and should be disclosed in your presentation
of the project too — it's a stand-in for real cost data, not a claim about
Meridian Retail's actual margins.

Then test the alert logic:

```bash
python src/check_alerts.py
```

If nothing is flagged, that's normal — it depends on whether any product's
margin genuinely dropped more than 15% between the two most recent weeks
in the dataset. To see the alert path work end-to-end at least once while
testing, you can temporarily lower `MARGIN_DROP_THRESHOLD_PCT` in
`check_alerts.py` to something small like `1.0`, confirm it fires, then set
it back.

---

## Step 6 — Create a Slack webhook

**Process (no code required):**
1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**.
2. Name it (e.g. "Retail Alerts") and pick a workspace — your own personal
   Slack workspace is fine if you don't have a work one to use.
3. In the app settings sidebar, click **Incoming Webhooks** → toggle it **On**.
4. Click **Add New Webhook to Workspace**, choose a channel (e.g. `#retail-alerts`),
   and click **Allow**.
5. Copy the webhook URL it gives you (starts with `https://hooks.slack.com/services/...`).
6. Paste it into your local `.env` as `SLACK_WEBHOOK_URL` and re-run
   `python check_alerts.py` (with the threshold temporarily lowered, per
   Step 5) to confirm a message actually lands in your Slack channel.

---

## Step 7 — (Optional) Add the Anthropic API for written recommendations

If you set `ANTHROPIC_API_KEY` in `.env`, `check_alerts.py` will call the
Claude API to turn the raw margin numbers into a short written
recommendation instead of a templated sentence. This is optional — the
script works fine without it, just with a plainer message.
Get a key at [console.anthropic.com](https://console.anthropic.com).

---

## Step 8 — Push to GitHub and set repository secrets

**Process (no code required):**
1. Push your repo to GitHub if you haven't already.
2. Go to your repo → **Settings** → **Secrets and variables** → **Actions**.
3. Click **New repository secret** and add each of these one at a time:
   - `DATABASE_URL` → your Neon connection string
   - `SLACK_WEBHOOK_URL` → your Slack webhook URL
   - `ANTHROPIC_API_KEY` → your Anthropic API key (skip if not using this)
   - `DATA_PATH` → only needed if your data file isn't at the default
     `data/online_retail_ii.csv`

Secrets are never visible in logs or to other people viewing your repo —
this is the correct way to store credentials for a public portfolio project.

---

## Step 9 — Turn on the automation

The workflow file (`.github/workflows/pipeline.yml`) is already in your
repo, so GitHub Actions will pick it up automatically once you push. To
confirm it's working without waiting for the daily schedule:

1. Go to your repo → **Actions** tab.
2. Click **retail-pipeline** in the left sidebar.
3. Click **Run workflow** (this uses the `workflow_dispatch` trigger
   already included in the YAML) → **Run workflow**.
4. Watch it run — you'll see live logs for both the ETL step and the
   alert-check step. A green checkmark means it completed successfully.

This is the artifact you point to in interviews: a real, scheduled,
publicly-visible automation run — not a claim in a README.

---

## Step 10 — Run the dashboard (Streamlit)

**Local test first:**
1. Create `.streamlit/secrets.toml` in your project root with:
   ```toml
   DATABASE_URL = "postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require"
   ```
   (this file is git-ignored — never commit it)
2. Run:
   ```bash
   streamlit run src/dashboard.py
   ```
   This opens the dashboard at `localhost:8501`, reading live from Neon.

**Deploy free on Streamlit Community Cloud:**
1. Push `src/dashboard.py` and the updated `requirements.txt` to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io) → sign in with GitHub.
3. **New app** → select your repo → set main file path to `src/dashboard.py`.
4. Under **Advanced settings → Secrets**, paste the same `DATABASE_URL = "..."` line as above.
5. **Deploy** — you get a public URL (e.g. `yourapp.streamlit.app`). Because it queries Neon live, it reflects new data automatically after every pipeline run, with no manual republishing.

---

## Step 11 — Historical backfill and incremental loads

The warehouse keeps historical years and adds new data; it never cycles back
through old rows. On the first ETL run, `CHUNK_DAYS=0` (the default) loads
the full available history, so the dashboard can immediately show every year
in the source file. `etl_control` records the latest loaded source date.

On later runs, the pipeline only selects source dates newer than that date.
Each fact row also has a deterministic source key with a database uniqueness
constraint, so re-running the same input cannot duplicate revenue, profit,
or orders. If a newer source file is placed at `DATA_PATH`, its newer rows
are appended while the earlier years remain available for comparison.

The included Online Retail II file is static (2009–2011), so daily runs will
correctly report that there are no newer rows until the source is replaced or
extended. To demonstrate a gradual initial load instead, set `CHUNK_DAYS` to
a positive number such as `30`; each run will then add the next date range
once, with no wrap-around or repeated history.

**Schema update:** the revised fact table adds `source_row_key` and the alert
log records one margin alert per product and analysis period. If your
database was created with an earlier schema, run the setup command once:
```bash
python src/setup_db.py
```
It intentionally drops and recreates this demo warehouse, so take a backup
first if you need to retain an existing database.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `RuntimeError: DATABASE_URL is not set` | `.env` wasn't loaded into your shell, or the GitHub secret name doesn't match exactly |
| `psycopg2.OperationalError: SSL` | Missing `?sslmode=require` at the end of the connection string |
| ETL runs but `fact_sales` is empty | Check `DATA_PATH` points to the actual file, and that the column names in your file match the `rename_map` in `etl.py` |
| `check_alerts.py` never fires | Expected most days — lower the threshold temporarily to confirm the path works, then restore it |
| GitHub Actions run fails on `pip install` | Check `requirements.txt` was committed and the workflow's `working-directory` matches your folder layout |
| Grafana shows "no data" | Confirm the data source's SSL mode is `require`, and that you ran the ETL at least once so `fact_sales` isn't empty |


