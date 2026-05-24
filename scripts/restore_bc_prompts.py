#!/usr/bin/env python3
"""Restore columns B and C of the Feishu prompt sheet"""
import requests, json

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                  json={"app_id": APP_ID, "app_secret": APP_SECRET})
token = r.json()["tenant_access_token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
sheet_token = "IRFqsUM7Jh4Hybt96ZVc9e0Antc"
url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values"

# 读取现有 Row 2
r1 = requests.get(f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/0Uq2PS!A2:G2", headers=headers)
vals = r1.json().get("data", {}).get("valueRange", {}).get("values", [[]])[0]

existing_a = vals[0] if len(vals) > 0 else ""
existing_d = vals[3] if len(vals) > 3 else ""
existing_e = vals[4] if len(vals) > 4 else ""
existing_f = vals[5] if len(vals) > 5 else ""
existing_g = vals[6] if len(vals) > 6 else ""

# Col B: 新建配置文件
col_b = """【目标】创建 AdsPower 指纹配置，美国 SOCKS5 代理

【步骤】
1. AdsPower → 新建配置
2. 系统: Windows
3. 代理: SOCKS5 gate.rola.vip:2000
   用户名: gyd602_xxxx-country-us-state-ca  (xxxx=随机4位，每次不同)
   密码: C0eLGm
4. 配置名称: 品牌-用途-序号 (如: KYLEE-reg-001)
5. 保存后记到飞书注册资料表

【IP刷新】
• 访问: refresh.rola.vip/refresh?user=gyd602_{ID}&country=us&state=ca
• 修改{ID}即可刷新

【验证】打开网站确认IP为美国，WebRTC不泄露"""

# Col C: 新建域名邮箱
col_c = """【目标】购买.shop域名绑定老薛主机，创建临时邮箱

【前置条件】
• Namesilo API Key 有效
• 老薛主机面板可登录
• 如果注册资料已有邮箱 → 跳过此步骤

【步骤】
1. Namesilo API 购买 .shop 域名 (~$2-3/个)
2. 老薛 cPanel → 域名管理 → 绑定到根目录
3. cPanel → 电子邮件 → 创建邮箱
   - 用户名: FirstName(重名用LastName) | 密码: 统一密码
4. 一域名最多4邮箱
5. 邮箱+密码保存到飞书注册资料表

【注意】仅用于Shopify验证邮件"""

body = {
    "valueRange": {
        "range": "0Uq2PS!A2:G2",
        "values": [[existing_a, col_b, col_c, existing_d, existing_e, existing_f, existing_g]]
    }
}
r2 = requests.put(url, headers=headers, json=body)
print(f"Update: {r2.json().get('msg')} (cells={r2.json().get('data',{}).get('updatedCells')})")

# Verify
print("\n验证:")
r3 = requests.get(url.replace("/values", "/values/0Uq2PS!A2:G2"), headers=headers)
vals2 = r3.json().get("data", {}).get("valueRange", {}).get("values", [[]])[0]
labels = ["流程总纲","新建配置","域名邮箱","注册Syncee","养号","Payment","SOP配置"]
for i, (label, val) in enumerate(zip(labels, vals2)):
    ok = len(str(val)) > 15
    print(f"  [{i+1}] {label}: {'✅' if ok else '❌'} ({str(val)[:40]})")
