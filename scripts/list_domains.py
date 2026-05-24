#!/usr/bin/env python3
"""List existing domains and try to find domain management"""
import requests, re, urllib3
urllib3.disable_warnings()

HOST = "https://104.129.59.5:2083"
session = requests.Session()
session.verify = False

resp = session.post(f"{HOST}/login/", data={"user": "familyfi", "pass": "6V)lIE8m;k3h0T"}, allow_redirects=True)
cpsess = re.search(r'/cpsess(\d+)/', resp.url)
cpsess = cpsess.group(1) if cpsess else None
print(f"CPSESS: {cpsess}")

api_base = f"{HOST}/cpsess{cpsess}/json-api/cpanel"

# List existing domains via Email (show which domains have emails)
resp = session.post(api_base, params={
    "cpanel_jsonapi_module": "Email",
    "cpanel_jsonapi_func": "listpops",
    "cpanel_jsonapi_apiversion": "2",
}, timeout=15)

if resp.status_code == 200:
    try:
        data = resp.json().get("cpanelresult", {}).get("data", [])
        domains = set()
        for pop in data:
            email = pop.get("email", "")
            if "@" in email:
                domains.add(email.split("@")[1])
        print(f"\n已有域名的邮箱 ({len(domains)}个域名):")
        for d in sorted(domains):
            print(f"  {d}")
    except Exception as e:
        print(f"Parse error: {e}")
        print(resp.text[:300])

# Try to see if we can use WHM API (port 2087)
print(f"\n=== Try WHM API ===")
for whm_port in [2087, 2086]:
    resp = requests.post(
        f"https://104.129.59.5:{whm_port}/login/",
        data={"user": "familyfi", "pass": "6V)lIE8m;k3h0T"},
        verify=False, timeout=10
    )
    print(f"  WHM port {whm_port}: {resp.status_code}, url={resp.url[:80]}")

# Also check if there's a cPanel API 3 that works for domain
print(f"\n=== Try API 2 with known working functions ===")
for module, func, data in [
    ("Email", "listpops", {}),
    ("Email", "listlisters", {}),
    ("Email", "listmail", {}),
    ("Email", "list_domains", {}),
]:
    resp = session.post(api_base, params={
        "cpanel_jsonapi_module": module,
        "cpanel_jsonapi_func": func,
        "cpanel_jsonapi_apiversion": "2",
    }, timeout=10)
    try:
        j = resp.json()
        err = j.get("cpanelresult", {}).get("error", "")
        if "Could not find" not in err:
            print(f"  {module}/{func}: WORKS!")
        else:
            print(f"  {module}/{func}: {err[:60]}")
    except:
        print(f"  {module}/{func}: parse error")
