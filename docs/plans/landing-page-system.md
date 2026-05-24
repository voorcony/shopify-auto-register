# WooCommerce + 静态落地页系统 实施计划

> **For Hermes:** Use Cursor Agent + subagent-driven-development to implement this plan.
> 所有部署目标：43.130.2.243 (小助理服务器, ubuntu/ZHOUjiahao1!)
> 落地页主域名：待定（用户有 cesomail.com）

**目标：** 构建一套 WooCommerce 后台管产品 + 纯静态前端落地页的系统，支持多产品多层变体选择、同页面加购、弹窗收集号码、防爬虫、同URL内容切换、对接Shopify Cover Product结算。

**架构：**
```
WooCommerce (Docker, 内网) → 定时同步脚本 → 静态JSON → Nginx + 静态落地页 HTML
                                                                   ↓
                                                          防爬虫/PC检测
                                                                   ↓
                                                          用户选品加购
                                                                   ↓
                                                          弹窗收集号码
                                                                   ↓
                                                      Shopify Cover Product Checkout
```

**Tech Stack:** Docker + WooCommerce, Python 同步脚本, Alpine.js + Tailwind (CDN) 静态前端, Nginx 反代

---

### Task 1: WooCommerce Docker 部署到 43.130.2.243

**Objective:** 在小助理服务器上部署 WooCommerce + MySQL，仅供内网访问管理产品。

**Files:**
- Create: `~/woocommerce/docker-compose.yml`
- Create: `~/woocommerce/nginx.conf`

**Step 1: 创建 Docker Compose**

```yaml
# ~/woocommerce/docker-compose.yml
version: '3'
services:
  db:
    image: mysql:8.0
    volumes:
      - db_data:/var/lib/mysql
    restart: always
    environment:
      MYSQL_ROOT_PASSWORD: rootpassword
      MYSQL_DATABASE: woocommerce
      MYSQL_USER: wordpress
      MYSQL_PASSWORD: wordpress
    networks:
      - wp_net

  wordpress:
    depends_on:
      - db
    image: wordpress:latest
    ports:
      - "127.0.0.1:8080:80"
    restart: always
    environment:
      WORDPRESS_DB_HOST: db:3306
      WORDPRESS_DB_USER: wordpress
      WORDPRESS_DB_PASSWORD: wordpress
      WORDPRESS_DB_NAME: woocommerce
    volumes:
      - wp_data:/var/www/html
    networks:
      - wp_net

volumes:
  db_data:
  wp_data:

networks:
  wp_net:
```

**Step 2: 启动**

```bash
cd ~/woocommerce
docker-compose up -d
# 访问 http://127.0.0.1:8080 完成 WordPress 安装
# 安装 WooCommerce 插件 + 导入演示产品数据
```

**Step 3: 配 WooCommerce REST API**

- WordPress 后台 → WooCommerce → 设置 → 高级 → REST API
- 添加一个只读 API Key (Consumer Key + Secret)
- 记下 key/secret，用于同步脚本

---

### Task 2: 产品同步脚本

**Objective:** Python 脚本定时从 WC REST API 拉取产品数据，生成静态 JSON 供前端使用。

**Files:**
- Create: `~/landing-backend/sync_products.py`
- Create: `~/landing-backend/requirements.txt`

**Step 1: 同步脚本**

```python
#!/usr/bin/env python3
"""
从 WooCommerce REST API 同步产品数据到静态 JSON
可被 cron 定时调用，或将变化推送到 CDN
"""
import json
import os
import sys
import requests
from datetime import datetime

# ====== 配置 ======
WC_URL = "http://127.0.0.1:8080"  # WooCommerce 内网地址
WC_KEY = "ck_xxx"      # 替换为你的 Consumer Key
WC_SECRET = "cs_xxx"   # 替换为你的 Consumer Secret
OUTPUT_DIR = "/var/www/landing/data"
# =================

def fetch_products():
    """从 WooCommerce 获取所有产品"""
    page = 1
    products = []
    while True:
        resp = requests.get(
            f"{WC_URL}/wp-json/wc/v3/products",
            auth=(WC_KEY, WC_SECRET),
            params={
                "per_page": 100,
                "page": page
            }
        )
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} {resp.text}")
            break
        data = resp.json()
        if not data:
            break
        products.extend(data)
        page += 1
    return products

def transform_products(products):
    """转换为前端友好的结构"""
    transformed = []
    for p in products:
        if p.get("type") == "variable":
            # 可变产品 - 获取变体
            variants = []
            for v in p.get("variations", []):
                variant_resp = requests.get(
                    f"{WC_URL}/wp-json/wc/v3/products/{p['id']}/variations/{v}",
                    auth=(WC_KEY, WC_SECRET)
                )
                if variant_resp.status_code == 200:
                    vd = variant_resp.json()
                    variants.append({
                        "id": vd["id"],
                        "sku": vd.get("sku", ""),
                        "price": float(vd.get("price", 0)),
                        "regular_price": float(vd.get("regular_price", 0)),
                        "sale_price": float(vd.get("sale_price", 0)) if vd.get("sale_price") else None,
                        "stock_status": vd.get("stock_status", "instock"),
                        "attributes": {a["name"]: a["option"] for a in vd.get("attributes", [])},
                        "image": vd.get("image", {}).get("src", p.get("images", [{}])[0].get("src", "")) if vd.get("image") else p.get("images", [{}])[0].get("src", "")
                    })
            
            # 提取属性的层级关系
            attributes = []
            for attr in p.get("attributes", []):
                attributes.append({
                    "name": attr["name"],
                    "options": attr["options"],
                    "variation": attr.get("variation", False)
                })
            
            transformed.append({
                "id": p["id"],
                "name": p["name"],
                "slug": p.get("slug", ""),
                "type": "variable",
                "description": p.get("short_description", ""),
                "images": [img["src"] for img in p.get("images", [])],
                "attributes": attributes,
                "variants": variants,
                "categories": [c["name"] for c in p.get("categories", [])]
            })
        elif p.get("type") == "simple":
            # 简单产品
            transformed.append({
                "id": p["id"],
                "name": p["name"],
                "slug": p.get("slug", ""),
                "type": "simple",
                "description": p.get("short_description", ""),
                "price": float(p.get("price", 0)),
                "regular_price": float(p.get("regular_price", 0)),
                "sale_price": float(p.get("sale_price", 0)) if p.get("sale_price") else None,
                "images": [img["src"] for img in p.get("images", [])],
                "stock_status": p.get("stock_status", "instock"),
                "sku": p.get("sku", ""),
                "categories": [c["name"] for c in p.get("categories", [])]
            })
    return transformed

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    products = fetch_products()
    transformed = transform_products(products)
    
    output = {
        "last_updated": datetime.now().isoformat(),
        "total_products": len(transformed),
        "products": transformed
    }
    
    # 写完整数据
    with open(f"{OUTPUT_DIR}/products.json", "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    # 写精简版（只含前端需要的字段）
    # 同时按分类拆分文件
    categories = {}
    for p in transformed:
        for cat in p.get("categories", []):
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(p)
    
    for cat, items in categories.items():
        safe_name = cat.replace(" ", "_").lower()
        with open(f"{OUTPUT_DIR}/category_{safe_name}.json", "w") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    
    # 写分类列表
    with open(f"{OUTPUT_DIR}/categories.json", "w") as f:
        json.dump(list(categories.keys()), f, ensure_ascii=False, indent=2)
    
    print(f"Synced {len(transformed)} products to {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
```

**Step 2: 创建 requirements.txt**

```
requests==2.31.0
```

**Step 3: 定时同步**

```bash
# 添加 cron，每5分钟同步一次
*/5 * * * * cd ~/landing-backend && python3 sync_products.py >> /var/log/product_sync.log 2>&1
```

---

### Task 3: 落地页 HTML 模板

**Objective:** 用 Alpine.js + Tailwind CSS 创建完整的静态落地页。

**Files:**
- Create: `~/landing-frontend/landing.html` — 真正的落地页
- Create: `~/landing-frontend/review.html` — 过审用的普通页面
- Create: `~/landing-frontend/js/app.js` — 主要逻辑
- Create: `~/landing-frontend/js/cart.js` — 购物车逻辑
- Create: `~/landing-frontend/js/checkout.js` — Shopify结算逻辑
- Create: `~/landing-frontend/css/style.css` — 自定义样式

**Step 1: 核心结构 — landing.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>奢华礼品精选</title>
  
  <!-- Anti-crawler JS check -->
  <script>
    // 浏览器检测 - 防爬虫
    (function() {
      // 检测是否有浏览器环境特征
      var is_bot = !window.navigator || 
                    !window.document || 
                    typeof window.navigator.webdriver !== 'undefined' ||
                    window.navigator.userAgent.includes('HeadlessChrome') === false;
      
      // 检测是否是真实浏览器
      if (typeof window === 'undefined' || 
          typeof document === 'undefined' ||
          typeof navigator === 'undefined') {
        // 是爬虫或非浏览器环境
        window.location.href = '/review';
        return;
      }
      
      // 检测屏幕尺寸 - 防PC
      // 如果是大屏设备，可能不是手机用户
      var screenWidth = window.screen.width;
      var isMobile = screenWidth < 1024 || !window.matchMedia('(pointer: fine)').matches;
      
      // 保存检测结果给后续逻辑
      window.__env = {
        isBot: false,
        isMobile: isMobile || true, // 默认允许手机访问
        isPC: !isMobile
      };
      
      // 如果是PC且没有特殊标记，显示普通页面
      if (window.__env.isPC) {
        // PC端跳转到普通页面
        var currentPath = window.location.pathname;
        if (!currentPath.startsWith('/review')) {
          window.location.href = '/review.html';
        }
      }
    })();
  </script>
  
  <!-- Alpine.js + Tailwind (CDN) -->
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  
  <!-- App logic -->
  <script defer src="/js/app.js"></script>
  <script defer src="/js/cart.js"></script>
  <script defer src="/js/checkout.js"></script>
  <link rel="stylesheet" href="/css/style.css">
  
  <!-- Cloudflare Turnstile (optional, for extra bot protection) -->
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head>
<body>
  <div x-data="landingApp()" x-init="init()" class="min-h-screen bg-gray-50">
    <!-- ===== 头部横幅 ===== -->
    <header class="bg-gradient-to-r from-purple-600 to-indigo-600 text-white py-6 px-4">
      <div class="max-w-4xl mx-auto text-center">
        <h1 class="text-3xl font-bold" x-text="pageTitle">奢华礼品精选</h1>
        <p class="mt-2 opacity-90" x-text="pageSubtitle">精选高端礼品，为您尊贵的客户</p>
      </div>
    </header>
    
    <main class="max-w-4xl mx-auto px-4 py-6">
      <!-- ===== 产品列表 ===== -->
      <div class="space-y-8">
        <template x-for="(product, pidx) in products" :key="product.id">
          <div class="bg-white rounded-xl shadow-md overflow-hidden">
            <!-- 产品标题 -->
            <div class="bg-gray-50 px-6 py-4 border-b">
              <h2 class="text-xl font-bold text-gray-800" x-text="product.name"></h2>
              <p class="text-gray-500 text-sm mt-1" x-text="product.description"></p>
            </div>
            
            <div class="p-6">
              <!-- 产品图片 -->
              <div class="flex -mx-2 mb-4 overflow-x-auto">
                <template x-for="(img, idx) in product.images" :key="idx">
                  <div class="flex-shrink-0 w-64 h-48 mx-2 bg-gray-100 rounded-lg overflow-hidden">
                    <img :src="img" :alt="product.name" class="w-full h-full object-cover">
                  </div>
                </template>
              </div>
              
              <!-- 多层变体选择器 -->
              <div class="space-y-4">
                <template x-for="(attr, aidx) in product.attributes" :key="aidx">
                  <div>
                    <label class="block text-sm font-medium text-gray-700 mb-2" x-text="attr.name"></label>
                    <div class="flex flex-wrap gap-2">
                      <template x-for="option in attr.options" :key="option">
                        <button
                          @click="selectVariant(product, attr.name, option)"
                          :class="{
                            'bg-indigo-600 text-white ring-2 ring-indigo-300': 
                              getSelectedAttribute(product.id, attr.name) === option,
                            'bg-white text-gray-700 border border-gray-300 hover:bg-gray-50':
                              getSelectedAttribute(product.id, attr.name) !== option
                          }"
                          class="px-4 py-2 rounded-lg text-sm font-medium transition-all"
                          x-text="option"
                        ></button>
                      </template>
                    </div>
                  </div>
                </template>
              </div>
              
              <!-- 选中的变体信息和价格 -->
              <div class="mt-4 p-4 bg-indigo-50 rounded-lg">
                <div class="flex justify-between items-center">
                  <div>
                    <p class="text-sm text-gray-600">
                      已选: <span class="font-medium" x-text="getSelectedSummary(product)"></span>
                    </p>
                    <p class="text-2xl font-bold text-indigo-600 mt-1">
                      ¥<span x-text="getSelectedPrice(product)"></span>
                    </p>
                  </div>
                  <button
                    @click="addToCart(product)"
                    :disabled="!canAddToCart(product)"
                    :class="canAddToCart(product) ? 'bg-indigo-600 hover:bg-indigo-700' : 'bg-gray-300 cursor-not-allowed'"
                    class="px-6 py-3 text-white font-medium rounded-lg transition-colors"
                  >
                    加入购物车
                  </button>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>
      
      <!-- ===== 购物车浮窗按钮 ===== -->
      <div 
        x-show="cartItems.length > 0" 
        x-transition
        class="fixed bottom-6 right-6 z-50"
      >
        <button 
          @click="showCart = !showCart"
          class="bg-indigo-600 text-white w-16 h-16 rounded-full shadow-lg hover:bg-indigo-700 transition-all flex items-center justify-center relative"
        >
          <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 100 4 2 2 0 000-4z" />
          </svg>
          <span class="absolute -top-1 -right-1 bg-red-500 text-white text-xs w-6 h-6 rounded-full flex items-center justify-center font-bold" x-text="cartItems.length"></span>
        </button>
      </div>
      
      <!-- ===== 购物车侧边栏 ===== -->
      <div 
        x-show="showCart" 
        @click.away="showCart = false"
        x-transition:enter="transition ease-out duration-300"
        x-transition:enter-start="translate-x-full"
        x-transition:enter-end="translate-x-0"
        x-transition:leave="transition ease-in duration-200"
        x-transition:leave-start="translate-x-0"
        x-transition:leave-end="translate-x-full"
        class="fixed inset-y-0 right-0 w-full max-w-md bg-white shadow-2xl z-50 flex flex-col"
      >
        <!-- 购物车头部 -->
        <div class="flex items-center justify-between px-6 py-4 border-b">
          <h2 class="text-lg font-bold">购物车</h2>
          <button @click="showCart = false" class="text-gray-400 hover:text-gray-600">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
            </svg>
          </button>
        </div>
        
        <!-- 购物车商品列表 -->
        <div class="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          <template x-for="(item, idx) in cartItems" :key="idx">
            <div class="flex items-center justify-between py-3 border-b border-gray-100">
              <div class="flex-1">
                <p class="font-medium text-gray-800" x-text="item.productName"></p>
                <p class="text-sm text-gray-500" x-text="item.variantSummary"></p>
                <p class="text-indigo-600 font-bold mt-1">¥<span x-text="item.price"></span></p>
              </div>
              <button @click="removeFromCart(idx)" class="text-red-400 hover:text-red-600 ml-4">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
                </svg>
              </button>
            </div>
          </template>
          
          <!-- 空购物车 -->
          <div x-show="cartItems.length === 0" class="text-center py-12 text-gray-400">
            <p>购物车是空的</p>
          </div>
        </div>
        
        <!-- 购物车底部：总计 + 结账按钮 -->
        <div x-show="cartItems.length > 0" class="border-t px-6 py-4 space-y-3">
          <div class="flex justify-between text-lg font-bold">
            <span>总计</span>
            <span class="text-indigo-600">¥<span x-text="cartTotal"></span></span>
          </div>
          <button 
            @click="showPhoneModal = true"
            class="w-full bg-green-600 hover:bg-green-700 text-white py-3 rounded-lg font-bold text-lg transition-colors"
          >
            立即结账
          </button>
        </div>
      </div>
      
      <!-- ===== 手机号弹窗 ===== -->
      <div 
        x-show="showPhoneModal" 
        class="fixed inset-0 bg-black bg-opacity-50 z-[100] flex items-center justify-center p-4"
        @click.away="showPhoneModal = false"
      >
        <div class="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
          <h3 class="text-xl font-bold text-center mb-2">请输入手机号</h3>
          <p class="text-sm text-gray-500 text-center mb-6">我们将在下单后与您联系确认</p>
          
          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">手机号码</label>
              <input 
                type="tel" 
                x-model="phoneNumber"
                placeholder="请输入手机号"
                maxlength="11"
                class="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500"
              >
            </div>
            
            <!-- Turnstile 验证 (可选) -->
            <div x-show="showTurnstile" id="turnstile-container"></div>
            
            <button 
              @click="submitOrder()"
              :disabled="!isValidPhone"
              :class="isValidPhone ? 'bg-indigo-600 hover:bg-indigo-700' : 'bg-gray-300 cursor-not-allowed'"
              class="w-full text-white py-3 rounded-lg font-bold transition-colors"
            >
              确认并前往支付
            </button>
            
            <p class="text-xs text-gray-400 text-center" x-show="isSubmitting">
              正在处理...
            </p>
          </div>
        </div>
      </div>
      
      <!-- ===== 加载状态 ===== -->
      <div x-show="loading" class="text-center py-20">
        <div class="inline-block animate-spin rounded-full h-8 w-8 border-4 border-indigo-600 border-t-transparent"></div>
        <p class="mt-2 text-gray-500">加载中...</p>
      </div>
    </main>
  </div>
</body>
</html>
```

**Step 2: app.js — Alpine.js 应用逻辑**

```javascript
// js/app.js
function landingApp() {
  return {
    // ===== 页面配置（同URL切换用） =====
    pageTitle: '奢华礼品精选',
    pageSubtitle: '精选高端礼品，为您尊贵的客户',
    
    // ===== 数据 =====
    products: [],
    cartItems: [],
    selectedAttributes: {},  // { productId: { attrName: option } }
    loading: true,
    showCart: false,
    showPhoneModal: false,
    phoneNumber: '',
    isSubmitting: false,
    showTurnstile: false,
    orderSessionId: null,
    
    // ===== 初始化 =====
    async init() {
      try {
        const resp = await fetch('/data/products.json');
        const data = await resp.json();
        this.products = data.products;
        this.pageTitle = data.page_title || this.pageTitle;
        this.pageSubtitle = data.page_subtitle || this.pageSubtitle;
        this.loading = false;
      } catch (e) {
        console.error('Failed to load products:', e);
        this.loading = false;
      }
    },
    
    // ===== 变体选择 =====
    selectVariant(product, attrName, option) {
      if (!this.selectedAttributes[product.id]) {
        this.selectedAttributes[product.id] = {};
      }
      this.selectedAttributes[product.id][attrName] = option;
      // 触发 Alpine 响应式更新
      this.selectedAttributes = {...this.selectedAttributes};
    },
    
    getSelectedAttribute(productId, attrName) {
      return this.selectedAttributes[productId]?.[attrName] || null;
    },
    
    getSelectedVariant(product) {
      const selected = this.selectedAttributes[product.id];
      if (!selected) return null;
      
      // 检查是否所有属性都已选择
      const variationAttrs = product.attributes.filter(a => a.variation !== false);
      for (let attr of variationAttrs) {
        if (!selected[attr.name]) return null;
      }
      
      // 匹配变体
      return product.variants.find(v => {
        return Object.entries(selected).every(([key, val]) => {
          return v.attributes[key] === val;
        });
      }) || null;
    },
    
    getSelectedPrice(product) {
      const variant = this.getSelectedVariant(product);
      if (variant) return variant.price.toFixed(2);
      
      // 如果没有选中变体，显示最低价
      if (product.type === 'variable' && product.variants.length > 0) {
        const prices = product.variants.map(v => v.price);
        return `起 ${Math.min(...prices).toFixed(2)}`;
      }
      return product.price?.toFixed(2) || '0.00';
    },
    
    getSelectedSummary(product) {
      const selected = this.selectedAttributes[product.id];
      if (!selected) return '请选择规格';
      return Object.values(selected).join(' / ');
    },
    
    canAddToCart(product) {
      if (product.type === 'simple') return true;
      const variant = this.getSelectedVariant(product);
      return variant !== null && variant.stock_status === 'instock';
    },
    
    // ===== 购物车操作 =====
    addToCart(product) {
      const variant = this.getSelectedVariant(product);
      if (!variant && product.type === 'variable') return;
      
      const cartItem = {
        productId: product.id,
        productName: product.name,
        variantId: variant?.id || product.id,
        variantSummary: this.getSelectedSummary(product),
        price: variant?.price || product.price,
        sku: variant?.sku || product.sku,
        attributes: this.selectedAttributes[product.id] || {}
      };
      
      this.cartItems.push(cartItem);
      this.showCart = true;
    },
    
    removeFromCart(index) {
      this.cartItems.splice(index, 1);
    },
    
    get cartTotal() {
      return this.cartItems.reduce((sum, item) => sum + item.price, 0).toFixed(2);
    },
    
    get isValidPhone() {
      return /^1[3-9]\d{9}$/.test(this.phoneNumber);
    },
    
    // ===== 结账 =====
    async submitOrder() {
      if (!this.isValidPhone || this.isSubmitting) return;
      this.isSubmitting = true;
      
      try {
        // 1. 生成订单ID (session_id)
        this.orderSessionId = 'ORD_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
        
        // 2. 保存订单信息到后端 (可选)
        const orderData = {
          session_id: this.orderSessionId,
          phone: this.phoneNumber,
          items: this.cartItems,
          total: this.cartTotal,
          timestamp: new Date().toISOString()
        };
        
        // 尝试保存到后端（不阻塞，如果后端不可用则走localStorage）
        try {
          await fetch('/api/save_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(orderData)
          });
        } catch (e) {
          console.warn('Backend save failed, using localStorage');
        }
        
        // 3. 本地备份
        localStorage.setItem('pending_order_' + this.orderSessionId, JSON.stringify(orderData));
        
        // 4. 计算总价 → 找到对应的Shopify Cover Product变体
        //    调用后端 API 获取 Shopify checkout URL
        //    如果后端不可用，走前端直连模式
        const totalPrice = parseFloat(this.cartTotal);
        
        // 根据总价选择对应的Shopify Cover Product变体
        const checkoutResp = await fetch('/api/create_checkout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: this.orderSessionId,
            total_price: totalPrice,
            phone: this.phoneNumber,
            items: this.cartItems
          })
        });
        
        if (checkoutResp.ok) {
          const checkoutData = await checkoutResp.json();
          // 跳转到 Shopify Checkout
          window.location.href = checkoutData.checkout_url;
        } else {
          // 降级方案：直接使用 cover product 的固定链接
          alert('正在跳转支付页面...');
          // 这里可以配置一个固定cover product链接+传参
        }
      } catch (e) {
        console.error('Checkout error:', e);
        alert('提交失败，请稍后重试');
      } finally {
        this.isSubmitting = false;
      }
    }
  };
}
```

**Step 3: cart.js — 购物车本地持久化**

```javascript
// js/cart.js (optional enhancement)
// 购物车同步到 localStorage，刷新不丢失
(function() {
  // 实际逻辑已集成在 app.js 的 Alpine 组件中
  // 这个文件可用于额外的购物车工具函数
})();
```

**Step 4: checkout.js — Shopify 结算模块**

```javascript
// js/checkout.js
// Shopify checkout 集成
// 负责：根据购物车总价找到对应 cover product → 跳转到 Shopify
const SHOPIFY_CONFIG = {
  store: 'your-store.myshopify.com',
  // Cover Product 变体列表
  // 在Shopify后台创建好后填入
  tiers: [
    { max_price: 150, variant_id: 'gid://shopify/ProductVariant/xxx1' },
    { max_price: 300, variant_id: 'gid://shopify/ProductVariant/xxx2' },
    { max_price: 500, variant_id: 'gid://shopify/ProductVariant/xxx3' },
    { max_price: 1000, variant_id: 'gid://shopify/ProductVariant/xxx4' },
    { max_price: 2000, variant_id: 'gid://shopify/ProductVariant/xxx5' },
  ]
};

function getTierForPrice(price) {
  const sorted = [...SHOPIFY_CONFIG.tiers].sort((a, b) => a.max_price - b.max_price);
  for (const tier of sorted) {
    if (price <= tier.max_price) return tier;
  }
  return sorted[sorted.length - 1];
}

function buildShopifyCheckoutUrl(sessionId, totalPrice) {
  const tier = getTierForPrice(totalPrice);
  // Shopify 直接加购链接格式
  return `https://${SHOPIFY_CONFIG.store}/cart/${tier.variant_id.split('/').pop()}:1?attributes[_session_id]=${sessionId}`;
}
```

**Step 5: style.css — 自定义样式补丁**

```css
/* css/style.css */
/* 变体选择按钮动画 */
.bg-indigo-600.text-white {
  transform: scale(1.05);
}

/* 购物车滑入动画 */
.fixed.inset-y-0 {
  transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

/* 图片懒加载占位 */
img[src=""] {
  opacity: 0;
}

/* 移动端优化 */
@media (max-width: 640px) {
  .text-3xl { font-size: 1.5rem; }
  .flex-wrap.gap-2 button {
    font-size: 0.8125rem;
    padding: 0.5rem 0.75rem;
  }
}
```

**Step 6: review.html — 过审用的普通页面**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>品牌展示 - 奢华礼品</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-white">
  <header class="bg-gray-100 py-12">
    <div class="max-w-4xl mx-auto text-center px-4">
      <h1 class="text-3xl font-bold text-gray-800">品牌展示</h1>
      <p class="mt-4 text-gray-600 max-w-2xl mx-auto">
        我们是一家专注高端礼品定制的品牌，为企业和个人提供臻选礼品解决方案。
      </p>
    </div>
  </header>
  
  <main class="max-w-4xl mx-auto px-4 py-12">
    <!-- 品牌介绍 -->
    <section class="mb-12">
      <h2 class="text-2xl font-bold mb-4">关于我们</h2>
      <p class="text-gray-600 leading-relaxed">
        成立于2020年，我们致力于为追求品质的客户提供独特的礼品体验。
        精选全球优质供应商，每件产品都经过严格筛选。
      </p>
    </section>
    
    <!-- 产品展示 -->
    <section>
      <h2 class="text-2xl font-bold mb-6">我们的产品</h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div class="border rounded-lg p-4">
          <div class="w-full h-40 bg-gray-200 rounded mb-3"></div>
          <h3 class="font-medium">经典系列</h3>
          <p class="text-sm text-gray-500">经典设计的礼品选择</p>
        </div>
        <div class="border rounded-lg p-4">
          <div class="w-full h-40 bg-gray-200 rounded mb-3"></div>
          <h3 class="font-medium">尊享系列</h3>
          <p class="text-sm text-gray-500">高端奢华的礼品体验</p>
        </div>
        <div class="border rounded-lg p-4">
          <div class="w-full h-40 bg-gray-200 rounded mb-3"></div>
          <h3 class="font-medium">定制系列</h3>
          <p class="text-sm text-gray-500">专属定制的礼品方案</p>
        </div>
      </div>
    </section>
  </main>
  
  <footer class="bg-gray-50 py-8 mt-12 text-center text-gray-400 text-sm">
    &copy; 2026 奢华礼品精选. All rights reserved.
  </footer>
</body>
</html>
```

---

### Task 4: Nginx 配置

**Objective:** 配置 Nginx 做反向代理、防爬虫、同URL切换、静态资源服务。

**Files:**
- Modify: `/etc/nginx/sites-available/landing`

**Step 1: Nginx 配置文件**

```nginx
# /etc/nginx/sites-available/landing
server {
    listen 80;
    server_name your-domain.com;
    
    root /var/www/landing;
    index landing.html;
    
    # ===== 防爬虫 =====
    # 已知爬虫 UA 一律返回 review 页面
    if ($http_user_agent ~* (Googlebot|Bingbot|Slurp|DuckDuckBot|Baiduspider|YandexBot|Sogou|Exabot|facebot|GPTBot|ChatGPT|CCBot|ClaudeBot|Applebot|facebookexternalhit|Twitterbot|rogerbot|linkedinbot|embedly|quora|pinterest|slack|whatsapp|curl|wget|python|java|go-http-client|ruby)) {
        rewrite ^ /review.html break;
    }
    
    # ===== 静态数据缓存 =====
    location /data/ {
        add_header Cache-Control "public, max-age=60";
        expires 1m;
    }
    
    location /js/ {
        add_header Cache-Control "public, max-age=3600";
        expires 1h;
    }
    
    location /css/ {
        add_header Cache-Control "public, max-age=3600";
        expires 1h;
    }
    
    # ===== 同步脚本调用（内网） =====
    location /api/ {
        # API 只允许内网或特定IP访问
        allow 127.0.0.1;
        deny all;
        
        proxy_pass http://127.0.0.1:8099;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
    
    # ===== 默认入口（同URL切换） =====
    # 审核阶段：root 目录放 review.html 为默认
    # 上线阶段：改为 landing.html 为默认
    # 切换只需修改或替换 index 指令
    
    # ===== Gzip =====
    gzip on;
    gzip_types text/html text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1000;
    gzip_comp_level 6;
    
    # ===== 安全头部 =====
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-XSS-Protection "1; mode=block" always;
}

# ===== HTTPS 跳转（如果有证书） =====
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    
    # 如果有 SSL 证书，配置 cert path
    # ssl_certificate /etc/ssl/certs/your-cert.pem;
    # ssl_certificate_key /etc/ssl/private/your-key.pem;
    
    # 同上配置...
}
```

**Step 2: 目录结构**

```
/var/www/landing/
├── landing.html          # 落地页（上线时启用）
├── review.html           # 普通页面（审核时启用）
├── data/
│   ├── products.json     # 同步脚本生成
│   ├── categories.json
│   └── category_*.json
├── js/
│   ├── app.js
│   ├── cart.js
│   └── checkout.js
└── css/
    └── style.css
```

---

### Task 5: Shopify Cover Product Checkout API

**Objective:** 后端 API 接收前端订单请求，查询 Cover Product 变体，生成 Shopify Checkout URL。

**Files:**
- Create: `~/landing-backend/checkout_api.py`
- Create: `~/landing-backend/requirements_api.txt`

**Step 1: FastAPI Checkout API**

```python
#!/usr/bin/env python3
"""Shopify Checkout API - 根据总价匹配 Cover Product，创建 Shopify 结算链接"""
import json
import os
import uuid
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

app = FastAPI(title="Landing Checkout API")

# ===== 配置 =====
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "your-store.myshopify.com")
SHOPIFY_STOREFRONT_TOKEN = os.getenv("SHOPIFY_STOREFRONT_TOKEN", "")
# Cover Product 变体映射（价格 → 变体GID）
COVER_TIERS = {
    150: "gid://shopify/ProductVariant/xxx1",
    300: "gid://shopify/ProductVariant/xxx2", 
    500: "gid://shopify/ProductVariant/xxx3",
    1000: "gid://shopify/ProductVariant/xxx4",
    2000: "gid://shopify/ProductVariant/xxx5",
}
# =================

class CheckoutRequest(BaseModel):
    session_id: str
    total_price: float
    phone: str
    items: list

class OrderRecord(BaseModel):
    session_id: str
    phone: str
    items: list
    total_price: float
    checkout_url: str = ""
    shopify_order_id: str = ""
    status: str = "pending"
    created_at: str = ""

# 内存存储（生产环境可改为 Redis/SQLite）
orders = {}

def get_tier_for_price(price: float) -> tuple[str, str]:
    """根据价格匹配最合适的 cover product 变体"""
    sorted_tiers = sorted(COVER_TIERS.keys())
    matched = sorted_tiers[0]
    for t in sorted_tiers:
        if price <= t:
            matched = t
            break
        matched = t
    return str(matched), COVER_TIERS[matched]

@app.post("/api/create_checkout")
async def create_checkout(req: CheckoutRequest):
    """创建 Shopify checkout 并返回结算链接"""
    # 1. 匹配 cover product 变体
    tier_price, variant_gid = get_tier_for_price(req.total_price)
    
    # 2. 存储订单信息
    order = OrderRecord(
        session_id=req.session_id,
        phone=req.phone,
        items=req.items,
        total_price=req.total_price,
        created_at=datetime.now().isoformat()
    )
    
    # 3. 尝试通过 Storefront API 创建 cart（如果配置了 token）
    cart_id = None
    checkout_url = None
    if SHOPIFY_STOREFRONT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                query = """
                mutation cartCreate($input: CartInput!) {
                    cartCreate(input: $input) {
                        cart { id checkoutUrl totalQuantity }
                        userErrors { field message }
                    }
                }
                """
                variables = {
                    "input": {
                        "lines": [{
                            "merchandiseId": variant_gid,
                            "quantity": 1
                        }],
                        "note": f"Session: {req.session_id}, Phone: {req.phone}"
                    }
                }
                resp = await client.post(
                    f"https://{SHOPIFY_STORE}/api/2024-10/graphql.json",
                    json={"query": query, "variables": variables},
                    headers={"X-Shopify-Storefront-Access-Token": SHOPIFY_STOREFRONT_TOKEN}
                )
                data = resp.json()
                if "data" in data and data["data"]["cartCreate"]["cart"]:
                    cart = data["data"]["cartCreate"]["cart"]
                    checkout_url = cart["checkoutUrl"]
                    cart_id = cart["id"]
        except Exception as e:
            print(f"Storefront API error: {e}")
    
    # 4. 降级方案：直接生成链接
    if not checkout_url:
        variant_id = variant_gid.split("/")[-1]
        checkout_url = f"https://{SHOPIFY_STORE}/cart/{variant_id}:1?attributes[_session_id]={req.session_id}"
    
    order.checkout_url = checkout_url
    order.status = "checkout_created"
    orders[req.session_id] = order
    
    return {
        "checkout_url": checkout_url,
        "session_id": req.session_id,
        "cart_id": cart_id
    }

@app.get("/api/order/{session_id}")
async def get_order(session_id: str):
    """查询订单状态"""
    order = orders.get(session_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.post("/api/webhook/shopify")
async def shopify_webhook(request: Request):
    """接收 Shopify 订单完成通知"""
    body = await request.body()
    data = json.loads(body)
    
    # 从订单 note 或 attribute 提取 session_id
    note = data.get("note", "")
    cart_token = data.get("cart_token", "")
    order_id = data.get("id", "")
    
    # 尝试从 note 中提取 session_id
    import re
    match = re.search(r"Session:\s*(\S+)", note)
    if match:
        session_id = match.group(1)
        if session_id in orders:
            orders[session_id].shopify_order_id = str(order_id)
            orders[session_id].status = "paid"
            print(f"Order {order_id} matched to session {session_id}")
    
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}
```

**Step 2: 启动 API**

```bash
cd ~/landing-backend
pip install -r requirements_api.txt
# Systemd service 或直接用 uvicorn
nohup uvicorn checkout_api:app --host 127.0.0.1 --port 8099 &
```

---

### Task 6: 部署到小助理服务器

**Objective:** 将本地开发好的文件部署到 43.130.2.243。

**Steps:**

```bash
# 1. 创建目录
sshpass -p 'ZHOUjiahao1!' ssh ubuntu@43.130.2.243 "mkdir -p /var/www/landing/{data,js,css} ~/landing-backend ~/woocommerce"

# 2. 上传文件
sshpass -p 'ZHOUjiahao1!' scp landing.html ubuntu@43.130.2.243:/var/www/landing/
sshpass -p 'ZHOUjiahao1!' scp review.html ubuntu@43.130.2.243:/var/www/landing/
sshpass -p 'ZHOUjiahao1!' scp -r js/* ubuntu@43.130.2.243:/var/www/landing/js/
sshpass -p 'ZHOUjiahao1!' scp -r css/* ubuntu@43.130.2.243:/var/www/landing/css/
sshpass -p 'ZHOUjiahao1!' scp sync_products.py ubuntu@43.130.2.243:~/landing-backend/
sshpass -p 'ZHOUjiahao1!' scp checkout_api.py ubuntu@43.130.2.243:~/landing-backend/
sshpass -p 'ZHOUjiahao1!' scp docker-compose.yml ubuntu@43.130.2.243:~/woocommerce/

# 3. 部署完成后的验证
curl http://localhost/landing.html  # 应返回完整落地页
curl -A "Googlebot" http://localhost/landing.html  # 应返回 review 页面
```

---

## 实施顺序

1. **Task 1** → WooCommerce Docker 部署
2. **Task 2** → 产品同步脚本（依赖 Task 1 有 API Key）
3. **Task 3** → 落地页 HTML 模板（可独立开发）
4. **Task 4** → Nginx 配置
5. **Task 5** → Shopify Checkout API
6. **Task 6** → 部署上线

---

## 防爬虫层级说明

| 层级 | 方法 | 效果 |
|------|------|------|
| L1 | Nginx UA 屏蔽 | 挡住已知爬虫 |
| L2 | JS 浏览器特征检测 | 挡住简单爬虫/脚本 |
| L3 | 屏幕尺寸检测 | 挡住 PC 访问 |
| L4 | Cloudflare Turnstile | 挡住高级爬虫（可选开启） |
| L5 | 同URL内容切换 | 审核/上线页面无感切换 |

## 验证标准

- [ ] WooCommerce 后台可登录，可编辑产品变体
- [ ] 同步脚本拉取产品 → 生成 products.json
- [ ] 落地页加载 products.json → 渲染多产品变体选择器
- [ ] 可多产品加购，购物车显示正确
- [ ] 点击结账 → 弹窗收集号码 → 跳转 Shopify
- [ ] 不同 UA 返回不同内容（爬虫/PC → review 页）
- [ ] 同URL切换：改 Nginx index 指向，秒级生效
