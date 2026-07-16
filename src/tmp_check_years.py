import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv('.env')
url = os.environ.get('DATABASE_URL')
print('DATABASE_URL_SET', bool(url))
if not url:
    raise SystemExit('DATABASE_URL missing')
engine = create_engine(url, pool_pre_ping=True)
with engine.connect() as conn:
    print('FACT_SALES_ROWS', conn.execute(text('select count(*) from fact_sales')).scalar())
    print('DATE_RANGE', conn.execute(text('select min(date_id), max(date_id) from fact_sales')).fetchall())
    print('YEARS', conn.execute(text("select distinct extract(year from date_id) as year from fact_sales where date_id is not null order by year")).fetchall())
