#!/usr/bin/env python3
"""Debug Feishu bitable access"""
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
print(f"Token: {token[:30]}...")
print(f"Token response: {json.dumps(data, indent=2, ensure_ascii=False)}")

headers = {"Authorization": f"Bearer {token}"}

# Try to get app info first - check if app exists
print("\n--- Testing bitable app access ---")
url1 = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
resp1 = requests.get(url1, headers=headers)
print(f"GET /bitable/v1/apps/{{token}}: {resp1.status_code}")
print(f"Response: {json.dumps(resp1.json(), indent=2, ensure_ascii=False)}")

# Try tables endpoint with different approaches
print("\n--- Testing tables access ---")
url2 = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables"
resp2 = requests.get(url2, headers=headers)
print(f"GET /bitable/v1/apps/{{token}}/tables: {resp2.status_code}")
print(f"Response: {json.dumps(resp2.json(), indent=2, ensure_ascii=False)}")

# Maybe the app_token is actually just the base token and we need to find tables differently
# Let me also check if the bitable might be in a different format
# Some bitables use app_token like this pattern

# Let's also try to see what scopes/permissions this token has
print("\n--- Checking token scopes ---")
url3 = "https://open.feishu.cn/open-apis/bitable/v1/apps"
resp3 = requests.get(url3, headers=headers)
print(f"GET /bitable/v1/apps: {resp3.status_code}")
print(f"Response: {json.dumps(resp3.json(), indent=2, ensure_ascii=False)}")

# Try to check if the app exists via drive API
print("\n--- Checking drive API ---")
url4 = f"https://open.feishu.cn/open-apis/drive/explorer/v2/file/{APP_TOKEN}"
resp4 = requests.get(url4, headers=headers)
print(f"GET /drive/explorer/v2/file/{{token}}: {resp4.status_code}")
print(f"Response: {json.dumps(resp4.json(), indent=2, ensure_ascii=False)}")
