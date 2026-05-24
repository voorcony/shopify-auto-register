#!/usr/bin/env python3
"""Create 4 email accounts on 老薛 cPanel"""
import requests, re, urllib3
urllib3.disable_warnings()

HOST = "https://104.129.59.5:2083"
USER = "familyfi"
PASS = "6V)lIE8m;k3h0T"
EMAIL_PASS = "4O6d&xuRDmyX"

session = requests.Session()
session.verify = False

# Login - get cpsess
resp = session.post(f"{HOST}/login/", data={"user": USER, "pass": PASS}, allow_redirects=True)
print(f"Login: {resp.status_code}")
print(f"Final URL: {resp.url}")

cpsess_match = re.search(r'/cpsess(\d+)/', resp.url)
cpsess = cpsess_match.group(1) if cpsess_match else None
print(f"CPSESS: {cpsess}")

if not cpsess:
    print("Failed to get session!")
    print(f"URL: {resp.url}")
    print(f"Cookies: {dict(session.cookies)}")
    exit(1)

# Use cpsess in URL for API calls
emails = ["admin@bagprism.shop", "support@bagprism.shop", "service@bagprism.shop", "sales@bagprism.shop"]

results = []
for email in emails:
    print(f"\nCreating {email}...")
    try:
        api_url = f"{HOST}/cpsess{cpsess}/json-api/cpanel"
        params = {
            "cpanel_jsonapi_module": "Email",
            "cpanel_jsonapi_func": "addpop",
            "cpanel_jsonapi_apiversion": "2",
        }
        resp = session.post(
            api_url,
            params=params,
            data={"email": email, "password": EMAIL_PASS, "quota": "250"},
            timeout=15
        )
        result = resp.json()
        err = result.get("cpanelresult", {}).get("error", "OK")
        if err == "OK":
            results.append((email, "✅"))
            print(f"  ✅ SUCCESS")
        else:
            results.append((email, f"❌ {err[:60]}"))
            print(f"  ❌ {err}")
    except Exception as e:
        results.append((email, f"❌ {str(e)[:60]}"))
        print(f"  ❌ {e}")

print("\n\n=== 最终结果 ===")
for email, status in results:
    print(f"  {status} {email}")
