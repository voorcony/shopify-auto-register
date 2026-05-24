#!/usr/bin/env python3
"""Try WHM API to add domain + create emails"""
import requests, re, urllib3
urllib3.disable_warnings()

HOST = "https://104.129.59.5"
SESSION = requests.Session()
SESSION.verify = False

# Try WHM login (port 2087)
resp = SESSION.post(f"{HOST}:2087/login/", 
    data={"user": "familyfi", "pass": "6V)lIE8m;k3h0T"},
    allow_redirects=True, timeout=15)
print(f"WHM login: status={resp.status_code}, url={resp.url[:80]}")
print(f"  text[100]: {resp.text[:100]}")

# Check for cpsess
cpsess = re.search(r'/cpsess(\d+)/', resp.url)
if cpsess:
    cpsess = cpsess.group(1)
    print(f"  CPSESS: {cpsess}")
    
    # Try WHM API to create a domain
    whm_api = f"{HOST}:2087/cpsess{cpsess}/json-api/"
    
    # WHM: createacct to create a new account, or addon domain
    # For reseller: addzonerecord, adddns, addon_domain
    for api_call, params in [
        ("add_domain", {"domain": "bagprism.shop", "user": "familyfi", "dir": "bagprism.shop"}),
        ("createacct", {"username": "bagprism", "domain": "bagprism.shop", "password": "4O6dxuRDmyX", "plan": "default"}),
    ]:
        resp2 = SESSION.get(f"{whm_api}{api_call}", params=params, timeout=15)
        print(f"\n  WHM/{api_call}: {resp2.status_code}")
        try:
            d = resp2.json()
            print(f"    {json.dumps(d, ensure_ascii=False)[:200]}")
        except:
            print(f"    {resp2.text[:200]}")
else:
    print("No WHM cpsess found")
    print(f"  cookies: {dict(SESSION.cookies)}")
