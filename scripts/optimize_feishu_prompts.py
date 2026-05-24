#!/usr/bin/env python3
"""Rewrite Feishu automation prompts with optimized structure"""
import requests, json

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"

# Get token
r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                  json={"app_id": APP_ID, "app_secret": APP_SECRET})
token = r.json()["tenant_access_token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

sheet_token = "IRFqsUM7Jh4Hybt96ZVc9e0Antc"

# New header row - rearranged in workflow order
header = [
    "流程总纲",
    "① 新建配置文件",
    "② 新建域名邮箱",
    "③ shopify注册 + Syncee安装",
    "④ shopify养号",
    "⑤ shopify payment注册",
    "⚙️ SOP自动注册配置",
]

# Row 2 content - carefully structured
col_a = """【全流程时间线】
第0天  新建配置文件 + 域名邮箱（可并行）
第0天  注册 Shopify + 安装 Syncee（间隔2小时/个）
第25~30天  养号（导入客户+创建订单）
养号后40小时  注册 Shopify Payment

【核心原则】
• 每个任务间隔至少2小时，不要批量操作
• 失败3次则放弃该步骤，标记状态到飞书
• 配置文件在 AdsPower 管理
• 所有操作通过 bu-30b (本地4090) 执行
• 每次操作前先检查已有浏览器标签页，避免重复打开"""

col_b = """【目标】
在 AdsPower 创建新的浏览器指纹配置，使用美国 SOCKS5 代理

【步骤】
1. 打开 AdsPower 桌面版 → 新建配置
2. 系统: Windows
3. 代理: SOCKS5 gate.rola.vip:2000
   用户名: gyd602_xxxx-country-us-state-ca (xxxx为随机4位字母数字)
   密码: C0eLGm
4. 配置名称格式: 品牌-用途-序号 (如: KYLEE-reg-001)
5. 保存后把名称记到飞书「注册资料」表

【IP刷新】
IP失效时访问: https://refresh.rola.vip/refresh?user=gyd602_{对应ID}&country=us&state=ca
修改 {对应ID} 即可刷新"""

col_c = """【目标】
购买 .shop 域名，绑定到老薛主机，创建临时邮箱用于注册

【步骤】
1. 用 Namesilo API 购买 .shop 域名 (~$2-3/个)
2. 登老薛 cPanel → 域名管理 → 绑定新域名到根目录
3. cPanel → 电子邮件账户 → 创建邮箱
   - 用户名: 注册资料的 FirstName (重名则用LastName)
   - 密码: 统一密码
4. 一个域名最多4个邮箱
5. 把邮箱地址+密码保存到飞书「注册资料」表"""

col_d = """【目标】
用域名邮箱注册 Shopify → 进 Dashboard → 安装 Syncee 导入商品

【前置检查】
• AdsPower 配置已启动或缓存了登录态
• Cloudflare 隧道通 (bu-30b可用)
• 先检查浏览器已有标签页,有Admin页面直接复用

【执行步骤】
1. 打开 shopify.com → 点击 Start Free Trial
2. 填注册资料:
   - 邮箱: 用新建的域名邮箱
   - 密码: 统一密码
   - 个人信息: 读取飞书注册资料表
3. 选 3-day Free Trial
   ⚠️ 绝不要输入信用卡信息
   ⚠️ 遇信用卡页面 → 找 Skip / Maybe later / 免费试用
   ⚠️ 找不到免费选项 → 刷新重试
4. 进入 Shopify Admin Dashboard
5. App Store → 搜索 Syncee AI Dropship → Install
6. 选 Free 试用 → 完成 3 步 Onboarding
7. 点击 Push all to store → 商品导入

【错误处理】
• 页面空白 → 等3秒刷新,最多3次
• CloudFlare → 交互验证
• 超时 → 刷新重试
• 有已打开的标签页 → 直接复用

【验证】
• 能访问 admin.shopify.com/store/{店铺名}
• Syncee 后台可见已导入商品"""

col_e = """【目标】
模拟正常经营行为,提高账号权重

【前置条件】
• 账号注册已超过 25 天
• 店铺可正常登录

【步骤】
1. 进 Shopify Dashboard
2. 导入 3-5 个客户:
   - 姓名地址用真实美国地址 (Google Maps搜)
   - 电话改后4位
3. 创建订单:
   - 选已导入的客户 → 选产品 → 获取付款 → Mark as Paid
4. 每个客户至少创建1个订单
5. 操作间隔不要太快

【验证】
• 订单列表可见 3-5 条已完成订单
• 完成后飞书标记: 已养好"""

col_f = """【目标】
注册 Shopify Payments,绑定收款

【前置条件】
• 已养好 (飞书标记已养好)
• 养号完成已超 40 小时

【步骤】
1. 进 Shopify Dashboard → 设置 → Payments
2. 注册 Shopify Payments
3. 企业类型: 个人独资企业 (Sole Proprietorship)
4. 填资料: 读取飞书注册资料表对应行
5. 如需邮箱验证码 → 登域名邮箱获取
6. 按向导逐步提交

【验证】
• Payments 页面显示激活
• 无风控警告
• 完成后飞书标记: 已注册payment"""

col_g = """【自动注册 SOP 配置】

触发: 我说"注册下一个" → 自动按 SOP 执行
数据: 飞书「注册资料」表 → 使用状态为空的配置
     飞书「自动化prompt」表 → 基线 prompt
引擎: bu-30b (本地RTX4090 via Cloudflare tunnel)
     browser-use 0.12.6 + AdsPower + SSH 隧道
学习: 失败步骤自动记录到
     /home/agentuser/shopify_experience.json
     下次执行自动注入经验补丁到 prompt
脚本: /home/agentuser/shopify_sop_runner.py"""

# ─── 写入 ──────────────────────────────────────

# 先清空
clear_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/0Uq2PS!A1:G10"
clear_body = {
    "valueRange": {
        "range": "0Uq2PS!A1:G10",
        "values": [["" for _ in range(7)] for _ in range(10)]
    }
}
r = requests.put(clear_url, headers=headers, json=clear_body)
print(f"Clear status: {r.status_code} text: {r.text[:200]}")

# 写新表头+内容
write_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values"
body = {
    "valueRange": {
        "range": "0Uq2PS!A1:G2",
        "values": [[
            col_a, col_b, col_c, col_d, col_e, col_f, col_g
        ]]
    }
}
# First write header
body2 = {
    "valueRange": {
        "range": "0Uq2PS!A1:G1",
        "values": [header]
    }
}
r = requests.put(write_url, headers=headers, json=body2)
print(f"Header: status={r.status_code} text: {r.text[:100]}")

# Then write content
r2 = requests.put(write_url, headers=headers, json=body)
print(f"Content: status={r2.status_code} text: {r2.text[:100]}")

# Verify
print("\n=== 验证写入结果 ===")
r3 = requests.get(
    f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/0Uq2PS!A1:G2",
    headers=headers)
try:
    data = r3.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    for i, row in enumerate(values):
        print(f"\n--- Row {i+1} ---")
        for j, cell in enumerate(row):
            preview = str(cell)[:80].replace('\n', ' | ')
            print(f"  Col {j+1}: {preview}")
except:
    print(f"Verify response: {r3.text[:200]}")
