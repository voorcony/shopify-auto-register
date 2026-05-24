#!/usr/bin/env python3
"""Find available modules and functions on 老薛 cPanel"""
import requests, re, urllib3, json
urllib3.disable_warnings()

HOST = "https://104.129.59.5:2083"
session = requests.Session()
session.verify = False

# Login
resp = session.post(f"{HOST}/login/", data={"user": "familyfi", "pass": "6V)lIE8m;k3h0T"}, allow_redirects=True)
cpsess = re.search(r'/cpsess(\d+)/', resp.url)
cpsess = cpsess.group(1) if cpsess else None
print(f"CPSESS: {cpsess}")

api_base = f"{HOST}/cpsess{cpsess}/json-api/cpanel"

# Modules we know exist (from earlier tests)
modules = ["Email", "DomainMod", "Market", "Domain", "cPAddons", "SiteHome", "NVData", "ModuleInfo", "Mysql", "Ftp", "SubDomain", "AddonDomain"]
working_funcs = {}

for module in modules:
    for func in ["listfuncs", "list_functions", "get_functions", "functions", "version"]:
        resp = session.post(api_base, params={
            "cpanel_jsonapi_module": module,
            "cpanel_jsonapi_func": func,
            "cpanel_jsonapi_apiversion": "2",
        }, timeout=10)
        try:
            j = resp.json()
            err = j.get("cpanelresult", {}).get("error", "")
            if "Could not find" not in err and "Token" not in err:
                data = j.get("cpanelresult", {}).get("data", {})
                print(f"\n{module}/{func} works!")
                print(f"  Data: {json.dumps(data, ensure_ascii=False)[:300]}")
                break
        except:
            pass
    else:
        # Module exists but no func listing - try known functions
        pass

# Try to find what Email module offers
print("\n=== Testing Email functions ===")
email_funcs = []
resp = session.post(api_base, params={
    "cpanel_jsonapi_module": "Email",
    "cpanel_jsonapi_func": "listpops",
    "cpanel_jsonapi_apiversion": "2",
}, timeout=10)
try:
    j = resp.json()
    err = j.get("cpanelresult", {}).get("error", "OK")
    if "Could not find" not in err:
        data = j.get("cpanelresult", {}).get("data", [])
        print(f"listpops returned {len(data)} items")
        for d in data[:3]:
            print(f"  {d.get('email', d)[:60]}")
except:
    print(f"  {resp.text[:100]}")

# Try reading the cPanel interface HTML to find what API is called when adding domains
print("\n=== Check domains page JavaScript ===")
dom_page = session.get(f"{HOST}/cpsess{cpsess}/frontend/jupiter/domains/index.html", timeout=10).text

# Search for cpanel_jsonapi references
import re
matches = re.findall(r'cpanel_jsonapi_module=["\\]?([^"\'&]+)', dom_page)
for m in sorted(set(matches)):
    print(f"  module={m}")

matches2 = re.findall(r'cpanel_jsonapi_func=["\\]?([^"\'&]+)', dom_page)
for m in sorted(set(matches2)):
    print(f"  func={m}")
