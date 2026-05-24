#!/usr/bin/env python3
"""Debug Feishu bitable - try different API approaches"""
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
print(f"Token obtained successfully")

headers = {"Authorization": f"Bearer " + token}
headers_json = {**headers, "Content-Type": "application/json"}

# Method 1: Try bitable v1 with additional format options
print("\n=== Method 1: Check if app_token needs special handling ===")
# Maybe the app_token is actually a file_token for the drive
url_drive = f"https://open.feishu.cn/open-apis/drive/v1/metas/batch_query"
resp_d = requests.post(url_drive, headers=headers_json, json={
    "request_docs": [{"doc_token": APP_TOKEN, "doc_type": "bitable"}]
})
print(f"Drive batch_query: {resp_d.status_code}")
try:
    print(json.dumps(resp_d.json(), indent=2, ensure_ascii=False))
except:
    print(resp_d.text[:500])

# Method 2: Try different bitable API format (should be same but let's try)
print("\n=== Method 2: Try bitable API with page_token=0 ===")
url_tables = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables?page_token=0&page_size=20"
resp_t = requests.get(url_tables, headers=headers)
print(f"Status: {resp_t.status_code}")
try:
    print(json.dumps(resp_t.json(), indent=2, ensure_ascii=False))
except:
    print(resp_t.text[:500])

# Method 3: Check if the app has bitable scope
print("\n=== Method 3: Check app permissions ===")
# Try to get the app's info
url_app = "https://open.feishu.cn/open-apis/application/v6/applications/cli_a9619830e2fadcd1"
resp_app = requests.get(url_app, headers=headers)
print(f"Application info: {resp_app.status_code}")
try:
    print(json.dumps(resp_app.json(), indent=2, ensure_ascii=False))
except:
    print(resp_app.text[:500])

# Method 4: Try bitable spreadsheet-style API
print("\n=== Method 4: Try spreadsheet-like API (sheets in bitable) ===")
url_sheets = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{APP_TOKEN}/sheets/0"
resp_s = requests.get(url_sheets, headers=headers)
print(f"Sheets API: {resp_s.status_code}")
try:
    print(json.dumps(resp_s.json(), indent=2, ensure_ascii=False))
except:
    print(resp_s.text[:500])

# Method 5: Try with the jssdk to see if API is accessible
print("\n=== Method 5: Check jssdk ticket (just for connectivity) ===")
url_js = "https://open.feishu.cn/open-apis/jssdk/ticket/get"
resp_js = requests.post(url_js, headers=headers_json)
print(f"JSSDK: {resp_js.status_code}")
try:
    print(json.dumps(resp_js.json(), indent=2, ensure_ascii=False))
except:
    print(resp_js.text[:500])
