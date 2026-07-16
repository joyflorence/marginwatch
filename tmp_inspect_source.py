from pathlib import Path
import pandas as pd

path = Path('data/online_retail_ii.xlsx')
sheets = pd.read_excel(path, sheet_name=None)
frames = []
for sheet_name, sheet in sheets.items():
    sheet = sheet.copy()
    sheet.columns = [c.strip().lower().replace(' ', '_') for c in sheet.columns]
    rename_map = {
        'invoice': 'invoice_no',
        'stockcode': 'stock_code',
        'customer_id': 'customer_id',
        'customerid': 'customer_id',
        'invoicedate': 'invoice_date',
        'unitprice': 'price',
    }
    sheet = sheet.rename(columns={k: v for k, v in rename_map.items() if k in sheet.columns})
    if 'invoice_date' in sheet.columns:
        sheet['invoice_date'] = pd.to_datetime(sheet['invoice_date'])
        frames.append(sheet)
full = pd.concat(frames, ignore_index=True)
print('rows', len(full))
print('date_min', full['invoice_date'].min())
print('date_max', full['invoice_date'].max())
print('years', sorted(full['invoice_date'].dt.year.unique().tolist()))
print(full['invoice_date'].dt.year.value_counts().sort_index().to_string())
