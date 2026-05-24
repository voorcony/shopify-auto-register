#!/usr/bin/env python3
"""Read Feishu spreadsheet data - shopify_products_rest_com"""
import requests
import json

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
APP_TOKEN = "JSfNsNXXJhFmoHtWsGHckJFrnNh"

# Get token
url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
data = resp.json()
token = data["tenant_access_token"]
print(f"[OK] Got tenant token")

headers = {"Authorization": f"Bearer " + token}

# The spreadsheet has sheet_id = "5ae9wc", 51 columns, 30 rows
sheet_id = "5ae9wc"

# Read all data - range A1 to max columns and rows
# Column 51 = AY, Row 30
range_str = f"{sheet_id}!A1:AY30"

print(f"\n[READING] Spreadsheet range: {range_str}")
url_values = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{APP_TOKEN}/values/{range_str}"
resp_v = requests.get(url_values, headers=headers)
print(f"Status: {resp_v.status_code}")

if resp_v.status_code == 200:
    result = resp_v.json()
    if result.get("code") == 0:
        value_range = result.get("data", {}).get("valueRange", {})
        values = value_range.get("values", [])
        print(f"[OK] Got {len(values)} rows of data")
        
        if values:
            # Print header row
            headers_row = values[0]
            print(f"\n{'='*80}")
            print(f"COLUMNS ({len(headers_row)} total):")
            print(f"{'='*80}")
            for i, h in enumerate(headers_row):
                print(f"  [{i}] {h}")
            
            # Print all data rows
            print(f"\n{'='*80}")
            print(f"DATA ROWS ({len(values)-1} records):")
            print(f"{'='*80}")
            
            for row_idx in range(1, len(values)):
                row = values[row_idx]
                print(f"\n--- Row {row_idx} ---")
                # Only print non-empty fields
                for col_idx, val in enumerate(row):
                    if val is not None and val != "":
                        col_name = headers_row[col_idx] if col_idx < len(headers_row) else f"Col{col_idx}"
                        val_str = str(val)
                        if len(val_str) > 200:
                            val_str = val_str[:200] + "..."
                        print(f"  {col_name}: {val_str}")
            
            # Summary of product names
            print(f"\n{'='*80}")
            print(f"PRODUCT NAMES SUMMARY")
            print(f"{'='*80}")
            name_col_idx = None
            for i, h in enumerate(headers_row):
                if h and any(k in h.lower() for k in ['title', 'name', '产品', '标题', 'product']):
                    name_col_idx = i
                    break
            if name_col_idx is not None:
                for row_idx in range(1, len(values)):
                    row = values[row_idx]
                    if name_col_idx < len(row):
                        print(f"  {row_idx}. {row[name_col_idx]}")
                    else:
                        print(f"  {row_idx}. (empty)")
        else:
            print("[WARN] No data found in the spreadsheet")
    else:
        print(f"[ERROR] API error: {json.dumps(result, indent=2, ensure_ascii=False)}")
else:
    print(f"[ERROR] HTTP error: {resp_v.status_code}")
    print(resp_v.text[:500])
