#!/usr/bin/env python3
"""Explore the document with given token - try sheets and other APIs"""
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
print(f"Token obtained")

headers = {"Authorization": f"Bearer " + token}
headers_json = {**headers, "Content-Type": "application/json"}

# Method 1: Try sheets API to list spreadsheet info
print("\n=== Sheets API: Get spreadsheet info ===")
url_spreadsheet = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{APP_TOKEN}"
resp_s = requests.get(url_spreadsheet, headers=headers)
print(f"Status: {resp_s.status_code}")
try:
    print(json.dumps(resp_s.json(), indent=2, ensure_ascii=False))
except:
    print(resp_s.text[:1000])

# Method 2: Try sheets API to list meta info
print("\n=== Sheets API: Get sheet meta ===")
url_meta = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{APP_TOKEN}/sheets/query"
resp_m = requests.get(url_meta, headers=headers)
print(f"Status: {resp_m.status_code}")
try:
    print(json.dumps(resp_m.json(), indent=2, ensure_ascii=False))
except:
    print(resp_m.text[:1000])

# Method 3: Try to get spreadsheet info (old API)
print("\n=== Old Sheets API: Get spreadsheet meta ===")
url_old = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{APP_TOKEN}/metainfo"
resp_old = requests.get(url_old, headers=headers)
print(f"Status: {resp_old.status_code}")
try:
    print(json.dumps(resp_old.json(), indent=2, ensure_ascii=False))
except:
    print(resp_old.text[:1000])

# Method 4: Check what bitable app might be accessible
print("\n=== Try to find bitables via search ===")
# Try search API
url_search = "https://open.feishu.cn/open-apis/search/v1/search"
# This probably won't work but let's try

# Method 5: Check what's the doc_type of this token
print("\n=== Drive API: Check document type ===")
url_doc = f"https://open.feishu.cn/open-apis/drive/v1/files/{APP_TOKEN}"
resp_doc = requests.get(url_doc, headers=headers)
print(f"Status: {resp_doc.status_code}")
try:
    print(json.dumps(resp_doc.json(), indent=2, ensure_ascii=False))
except:
    print(resp_doc.text[:500])

# Method 6: Try bitable API with different content-type or format
print("\n=== Bitable API: try with POST instead of GET ===")
url_bitable2 = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables"
resp_b2 = requests.post(url_bitable2, headers=headers_json, json={"page_size": 20})
print(f"Status: {resp_b2.status_code}")
try:
    print(json.dumps(resp_b2.json(), indent=2, ensure_ascii=False))
except:
    print(resp_b2.text[:500])
