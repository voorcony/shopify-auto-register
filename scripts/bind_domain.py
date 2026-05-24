#!/usr/bin/env python3
"""Bind domain + create emails on 老薛 cPanel"""
import requests, re, urllib3, json
urllib3.disable_warnings()

HOST = "https://104.129.59.5:2083"
USER = "familyfi"
PASS = "6V)lIE8m;k3h0T"
EMAIL_PASS = "4O6d&xuRDmyX"

session = requests.Session()
session.verify = False

# Step 1: Login
resp = session.post(f"{HOST}/login/", data={"user": USER, "pass": PASS}, allow_redirects=True)
cpsess_match = re.search(r'/cpsess(\d+)/', resp.url)
cpsess = cpsess_match.group(1) if cpsess_match else None
print(f"CPSESS: {cpsess}")
print(f"Cookies: {dict(session.cookies)}")

# Step 2: Try to bind domain - check ALL available APIs
# First try DomainMod
api_base = f"{HOST}/cpsess{cpsess}/json-api/cpanel"

# Try various domain-related API calls
for module, func, params in [
    ("DomainMod", "add_domain", {"domain": "bagprism.shop", "dir": "bagprism.shop"}),
    ("DomainMod", "add_domain_create_user", {"domain": "bagprism.shop", "dir": "bagprism.shop"}),
    ("DomainMod", "create_domain", {"domain": "bagprism.shop"}),
    ("DomainMod", "set_primary_domain", {"domain": "bagprism.shop"}),
    ("Market", "createSite", {"domain": "bagprism.shop", "dir": "bagprism.shop"}),
]:
    resp = session.post(api_base, params={
        "cpanel_jsonapi_module": module,
        "cpanel_jsonapi_func": func,
        "cpanel_jsonapi_apiversion": "2",
    }, data=params, timeout=15)
    try:
        j = resp.json()
        err = j.get("cpanelresult", {}).get("error", "OK")[:80]
        print(f"  {module}/{func}: {err}")
    except:
        print(f"  {module}/{func}: {resp.text[:80]}")

# Step 3: Also try UAPI domaininfo
print("\n=== UAPI attempts ===")
for endpoint, data in [
    ("/execute/DomainMod/add_domain", {"domain": "bagprism.shop", "dir": "bagprism.shop"}),
    ("/execute/DomainMod/create_domain", {"domain": "bagprism.shop"}),
    ("/execute/Market/create_site", {"domain": "bagprism.shop"}),
]:
    resp = session.post(f"{HOST}/cpsess{cpsess}{endpoint}", data=data, timeout=15)
    try:
        j = resp.json()
        err = j.get("errors", ["OK"])[0][:80]
        print(f"  {endpoint}: {err} (status={j.get('status')})")
    except:
        print(f"  {endpoint}: {resp.text[:100]}")
