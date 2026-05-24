#!/usr/bin/env python3
"""Rewrite Feishu automation prompts - clean version"""
import requests, json

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"

r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                  json={"app_id": APP_ID, "app_secret": APP_SECRET})
token = r.json()["tenant_access_token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
sheet_token = "IRFqsUM7Jh4Hybt96ZVc9e0Antc"
url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values"

row1 = [
    "流程总纲",
    "① 新建配置文件",
    "② 新建域名邮箱",
    "③ shopify注册 + Syncee安装",
    "④ shopify养号",
    "⑤ shopify payment注册",
    "⚙️ SOP自动注册配置",
]

row2_a = """【时间线】
第0天: 新建配置 + 域名邮箱（可并行）
第0天: 注册 Shopify + Syncee（间隔2h/个）
第25~30天: 养号（导入客户+创建订单）
养号后40h: 注册 Shopify Payment

【核心原则】
• 任务间隔至少2h，不批量操作
• 失败3次则放弃，标记飞书状态
• 每次操作前检查已有标签页，不复用"""

row2_b = """【目标】创建 AdsPower 指纹配置，美国 SOCKS5 代理

【步骤】
1. AdsPower → 新建配置
2. 系统: Windows / 代理: SOCKS5
3. 代理: gate.rola.vip:2000
   用户: gyd602_xxxx-country-us-state-ca
   密码: C0eLGm (xxxx为随机字符)
4. 名称: 品牌-用途-序号
5. 保存后记到飞书注册资料表

【IP刷新】refresh.rola.vip/refresh?user=gyd602_{ID}&country=us&state=ca"""

row2_c = """【目标】购买.shop域名，绑定老薛主机，建临时邮箱

【步骤】
1. Namesilo API 购买 .shop 域名
2. 老薛 cPanel → 域名管理 → 绑定根目录
3. cPanel → 电子邮件 → 创建邮箱
   - 用户名: FirstName（重名用LastName）
   - 密码: 统一密码
4. 一域名最多4邮箱
5. 保存到飞书注册资料表"""

row2_d = """【目标】注册 Shopify → 进 Dashboard → 安装 Syncee 导入商品

【前置检查】
• AdsPower 已启动或有缓存登录态
• Cloudflare 隧道通 (bu-30b可用)
• 先检查已有标签页，直接复用

【执行】
1. 打开 shopify.com → Start Free Trial
2. 填资料（从飞书资料表读取）
   - 邮箱: 域名邮箱 | 密码: 统一密码
   - 个人信息: FirstName LastName / 地址 / 电话
3. 选 3-day Free Trial
   ⚠️ 绝不要输信用卡
   ⚠️ 遇信用卡页 → Skip / Maybe later
4. 进 Admin Dashboard
5. App Store → Syncee AI Dropship → Install
6. Free试用 → 3步Onboarding → Push all to store

【错误处理】
• 页面空白 → 3秒后刷新×3
• CloudFlare → 交互验证
• 有已开标签页 → 直接复用
• 登录态缓存 → 直接进Dashboard

【验证】
• admin.shopify.com/store/{店名} 可达
• Syncee 后台有已导入商品"""

row2_e = """【目标】模拟真人经营，提高账号权重

【前置条件】注册超25天，可正常登录

【步骤】
1. 进 Shopify Dashboard
2. 导入 3-5 客户（真实美国地址+电话改尾号）
3. 创建订单：选客户→选商品→获取付款→Mark as Paid
4. 每客户至少1单，操作间隔自然

【完成后】飞书标记: 已养好"""

row2_f = """【目标】注册 Shopify Payments 绑定收款

【前置条件】已养好，且养号完成超40h

【步骤】
1. 设置 → Payments → 注册 Shopify Payments
2. 企业: Sole Proprietorship
3. 填资料（飞书注册资料表对应行）
4. 需验证码 → 登域名邮箱获取
5. 按向导提交

【完成后】飞书标记: 已注册payment"""

row2_g = """【触发】我说"注册下一个" → 自动执行
【数据】飞书注册资料表(使用状态为空) + prompt表(基线)
【引擎】bu-30b (本地4090) + browser-use 0.12.6 + AdsPower
【学习】失败自动记录到 experience.json，下次注入补丁
【脚本】/home/agentuser/shopify_sop_runner.py"""

# Write header + content in one shot
body = {
    "valueRange": {
        "range": "0Uq2PS!A1:G2",
        "values": [
            row1,   # Row 1: header
            [row2_a, row2_b, row2_c, row2_d, row2_e, row2_f, row2_g],  # Row 2: content
        ]
    }
}
r = requests.put(url, headers=headers, json=body)
print(f"Write: {r.json().get('msg')} (cells={r.json().get('data',{}).get('updatedCells')})")

# Verify
r2 = requests.get(
    f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/0Uq2PS!A1:G2",
    headers=headers)
data = r2.json()
values = data.get("data", {}).get("valueRange", {}).get("values", [])
for i, row in enumerate(values):
    print(f"\n--- Row {i+1} ---")
    for j, cell in enumerate(row):
        preview = str(cell)[:60].replace('\n', ' | ')
        print(f"  [{j+1}] {preview}")
