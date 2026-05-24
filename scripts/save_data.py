#!/usr/bin/env python3
"""Save all Feishu spreadsheet data to JSON"""
import requests
import json

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
APP_TOKEN = "JSfNsNXXJhFmoHtWsGHckJFrnNh"

# Get token
resp = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                     json={"app_id": APP_ID, "app_secret": APP_SECRET})
token = resp.json()["tenant_access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Read all data
sheet_id = "5ae9wc"
url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{APP_TOKEN}/values/{sheet_id}!A1:AY30"
resp = requests.get(url, headers=headers)
data = resp.json()
values = data.get("data", {}).get("valueRange", {}).get("values", [])

headers_row = values[0] if values else []

# Convert to structured records (group by Handle/Title)
records = []
current = None
for row in values[1:]:
    handle = row[0] if len(row) > 0 else None
    title = row[1] if len(row) > 1 else None
    
    if title:  # New product
        if current:
            records.append(current)
        current = {"Handle": handle, "Title": title, "Variants": [], "Images": []}
    
    # Build variant info
    variant = {}
    for i in range(14, 25):  # Variant columns
        if i < len(row) and row[i] and i < len(headers_row):
            variant[headers_row[i]] = row[i]
    if variant:
        current["Variants"].append(variant)
    
    # Build image info
    if len(row) > 25 and row[25]:
        img = {"Src": row[25], "Position": row[26] if len(row) > 26 else None}
        current["Images"].append(img)

if current:
    records.append(current)

# Save to file
output = {
    "sheet_name": "shopify_products_rest_com",
    "sheet_id": sheet_id,
    "columns": headers_row,
    "total_rows": len(values) - 1,
    "products": records
}

with open("/home/agentuser/shopify_products_data.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"[OK] Saved {len(records)} products to shopify_products_data.json")
for p in records:
    print(f"  Product: {p['Title']} - {len(p['Variants'])} variants, {len(p['Images'])} images")
