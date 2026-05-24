# WordPress 迁移计划 — B站 CMS 层

## 架构

```
A站 (静态LP) ──POST /api/orders──► B站 FastAPI (业务逻辑，保持不动)
                                       │
                                       ├── 启动时读配置 ←── WordPress REST API
                                       │     - Cover Product 变体映射 (tier_price → variant_gid)
                                       │     - Site 品牌设置
                                       │     - Redirect 配置
                                       │
                                       ├── 创建订单时写入 → WordPress REST API
                                       │     - Custom Post Type: shop_order
                                       │     - 字段: order_id, product_name, price, phone, status, checkout_url, shopify_order_id
                                       │
                                       └── 订单状态变更 → 更新 WordPress
```

## WordPress 插件

1. **Custom Post Type UI** — 创建 shop_order 和 shop_config 两种 CPT
2. **ACF (Advanced Custom Fields)** — 自定义字段

## WordPress 数据结构

### CPT: shop_config （单条记录，存所有配置）
- cover_tiers: JSON 字段，{price: variant_gid}
- site_name, tagline, redirect_message, redirect_delay, footer_text

### CPT: shop_order （每条订单一条）
- order_id (文本，唯一)
- product_name
- price (数字)
- customer_name
- customer_phone
- status (pending / checkout_created / paid / fulfilled / cancelled)
- checkout_url
- shopify_order_id
- created_at
- updated_at
- cover_product_id

## WordPress REST API 扩展

WordPress 端需要暴露的自定义端点：

### GET /wp-json/b-site/v1/config
返回 cover tiers 配置和 site 设置

### POST /wp-json/b-site/v1/orders
创建订单记录

### GET /wp-json/b-site/v1/orders
订单列表（给 admin dashboard 用）

### PUT /wp-json/b-site/v1/orders/{order_id}
更新订单状态

## FastAPI 端改动

### 新增 wp_client.py
- WordPress REST API 客户端
- 启动时拉取配置（替代现有 hardcoded LEGACY_COVER_PRODUCTS + SHOPIFY_STOREFRONT_TOKEN）
- 订单 CRUD 操作

### 修改 shopify.py
- 从 WordPress 获取 cover product 配置（替代 LEGACY_COVER_PRODUCTS + 动态 Shopify 查询）

### 修改 main.py
- 订单创建→同时写入 WordPress
- webhook 回调→更新 WordPress

### 停用 b-site-proxy-api (Payload CMS)
- WordPress 替代它的管理后台角色

## 文件名规范
- snake_case：Python 文件
- 表名/字段：snake_case
