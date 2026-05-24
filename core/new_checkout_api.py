#!/usr/bin/env python3
"""
checkout_api.py — A站 Local Checkout API

Handles Shopify checkout creation + Feishu order persistence directly.
Replaces the old thin proxy to B站 Vercel.

启动: uvicorn checkout_api:app --host 127.0.0.1 --port 8099
"""
import hashlib
import hmac
import json
import logging
import os
import random
import re
import string
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from math import ceil
from typing import Any, Optional

import httpx
import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

# Import Feishu config provider
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_api import get_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Config
# ---------------------------------------------------------------------------
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE", "147xvt-jc.myshopify.com")
STOREFRONT_TOKEN = os.getenv("SHOPIFY_STOREFRONT_TOKEN", "48382a763d8f47c5bf40b7983eeb2d73")
WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
API_AUTH_KEY = os.getenv("API_AUTH_KEY", "apk_b9a7c3d1e5f80")
UNIT_PRICE = float(os.getenv("UNIT_PRICE", "230"))

API_VERSION = "2024-10"
STOREFRONT_URL = f"https://{SHOPIFY_STORE}/api/{API_VERSION}/graphql.json"

# Feishu Bitable config
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a9619830e2fadcd1")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "kXdwL8yJZCDo9kwych0npgZ5W078RRkK")
FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "XKOCbEsKpaGRQgsrLB3c5vkFn2b")
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "tblqpeRTU7AHOohC")
FEISHU_BASE = "https://open.feishu.cn/open-apis"

# ---------------------------------------------------------------------------
# Known $230 variants (Full size) for Cooling Sheet Set
# ---------------------------------------------------------------------------
COOLING_SHEET_VARIANTS_230 = [
    {"id": "gid://shopify/ProductVariant/45624867651720", "title": "Aqua Blue / Full", "price": 230.00},
    {"id": "gid://shopify/ProductVariant/45624867684488", "title": "Arctic White / Full", "price": 230.00},
    {"id": "gid://shopify/ProductVariant/45624867717256", "title": "Cool Gray / Full", "price": 230.00},
    {"id": "gid://shopify/ProductVariant/45624867750024", "title": "Lavender Breeze / Full", "price": 230.00},
    {"id": "gid://shopify/ProductVariant/45624867782792", "title": "Snow Ivory / Full", "price": 230.00},
    {"id": "gid://shopify/ProductVariant/45624867815560", "title": "Midnight Blue / Full", "price": 230.00},
]

# In-memory order store (mirror of Feishu)
_orders: dict[str, dict] = {}
_record_ids: dict[str, str] = {}
_feishu_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="A-Station Checkout API (Local)",
    description="Checkout API — Shopify + Feishu, running on A站 port 8099",
    version="2.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class CreateCheckoutRequest(BaseModel):
    session_id: str
    total_price: float
    items: list = []
    refer: str = ""
    customer_name: str = ""


# ---------------------------------------------------------------------------
# Shopify Storefront API helpers
# ---------------------------------------------------------------------------
async def _storefront_query(query: str, variables: dict = None) -> dict:
    """Execute a GraphQL query against the Shopify Storefront API."""
    headers = {
        "X-Shopify-Storefront-Access-Token": STOREFRONT_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(STOREFRONT_URL, headers=headers, json=payload)
        logger.info("Storefront API response status: %s", resp.status_code)
        if resp.status_code != 200:
            logger.error("Storefront API error: %s - %s", resp.status_code, resp.text)
            resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            logger.error("GraphQL errors: %s", json.dumps(data["errors"]))
            raise Exception(f"GraphQL errors: {data['errors']}")
        return data


def select_random_matched_variant(target_price: float) -> dict:
    """
    Select a random $230 variant with the appropriate quantity to match target_price.
    """
    variants_pool = COOLING_SHEET_VARIANTS_230
    if not variants_pool:
        raise Exception("No $230 variants available for checkout")

    selected = random.choice(variants_pool)
    qty = max(1, ceil(target_price / UNIT_PRICE))
    total_price = qty * UNIT_PRICE

    return {
        "variant_gid": selected["id"],
        "variant_title": selected["title"],
        "qty": qty,
        "price": total_price,
    }


def calculate_price_match(price: float) -> dict:
    qty = max(1, ceil(price / UNIT_PRICE))
    total_before = qty * UNIT_PRICE
    discount = round(total_before - price, 2)
    return {
        "qty": qty,
        "unit_price": UNIT_PRICE,
        "total_before": total_before,
        "discount": discount,
    }


def get_unit_variant_gid() -> str:
    gid = os.getenv("UNIT_VARIANT_GID", "")
    if not gid:
        gid = "gid://shopify/ProductVariant/45624867651720"
    return gid


async def create_checkout_cart(
    variant_gid: str,
    order_id: str,
    quantity: int = 1,
    discount_code: Optional[str] = None,
    refer: str = "",
) -> dict:
    """Create a Shopify cart with the given variant and return checkout URL."""
    note_parts = [f"Order Reference: {order_id}"]
    if refer:
        note_parts.append(f"Refer: {refer}")

    create_mutation = """
    mutation cartCreate($input: CartInput!) {
      cartCreate(input: $input) {
        cart {
          id
          checkoutUrl
          totalQuantity
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {
        "input": {
            "lines": [
                {
                    "merchandiseId": variant_gid,
                    "quantity": quantity,
                }
            ],
            "note": " | ".join(note_parts),
        }
    }
    if discount_code:
        variables["input"]["discountCodes"] = [discount_code]

    result = await _storefront_query(create_mutation, variables)
    cart_data = result.get("data", {}).get("cartCreate", {})
    errors = cart_data.get("userErrors", [])
    if errors:
        error_msgs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors)
        raise Exception(f"Cart creation errors: {error_msgs}")

    cart = cart_data.get("cart")
    if not cart:
        raise Exception("No cart returned from Storefront API")

    logger.info("Cart created: %s, checkout URL: %s", cart["id"], cart.get("checkoutUrl"))
    return cart


def verify_webhook_hmac(body: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        logger.warning("No webhook secret configured, skipping HMAC verification")
        return True
    if not signature:
        logger.warning("No signature provided, skipping HMAC verification")
        return True

    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if signature.startswith("sha256="):
        signature = signature[7:]

    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Feishu token helpers
# ---------------------------------------------------------------------------
def _get_feishu_tenant_token() -> Optional[str]:
    """Return a cached tenant_access_token, refreshing 60s before expiry."""
    now = time.time()
    if _feishu_token_cache["token"] and now < _feishu_token_cache["expires_at"] - 60:
        return _feishu_token_cache["token"]

    try:
        resp = requests.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=8,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("Feishu token request failed: %s", data)
            return None
        token = data.get("tenant_access_token")
        expire = data.get("expire", 7200)
        _feishu_token_cache["token"] = token
        _feishu_token_cache["expires_at"] = now + float(expire)
        return token
    except Exception as e:
        logger.warning("Feishu token request error: %s", e)
        return None


def _feishu_auth_headers() -> Optional[dict[str, str]]:
    token = _get_feishu_tenant_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _bitable_url(suffix: str = "") -> str:
    return (
        f"{FEISHU_BASE}/bitable/v1/apps/{FEISHU_APP_TOKEN}"
        f"/tables/{FEISHU_TABLE_ID}/records{suffix}"
    )


def _feishu_create(order: dict) -> Optional[str]:
    headers = _feishu_auth_headers()
    if not headers:
        return None
    try:
        resp = requests.post(
            _bitable_url(),
            headers=headers,
            json={"fields": {
                "session_id": order.get("session_id", ""),
                "refer": order.get("refer", ""),
                "shopify_order_id": order.get("shopify_order_id", ""),
                "status": order.get("status", ""),
                "order_json": json.dumps(order, ensure_ascii=False, default=str),
            }},
            timeout=8,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("Feishu create record failed: %s", data)
            return None
        return ((data.get("data") or {}).get("record") or {}).get("record_id")
    except Exception as e:
        logger.warning("Feishu create record error: %s", e)
        return None


def _feishu_update(record_id: str, order: dict) -> bool:
    headers = _feishu_auth_headers()
    if not headers:
        return False
    try:
        resp = requests.put(
            _bitable_url(f"/{record_id}"),
            headers=headers,
            json={"fields": {
                "session_id": order.get("session_id", ""),
                "refer": order.get("refer", ""),
                "shopify_order_id": order.get("shopify_order_id", ""),
                "status": order.get("status", ""),
                "order_json": json.dumps(order, ensure_ascii=False, default=str),
            }},
            timeout=8,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("Feishu update record failed: %s", data)
            return False
        return True
    except Exception as e:
        logger.warning("Feishu update record error: %s", e)
        return False


def _feishu_search(field: str, value: str) -> Optional[tuple[str, dict]]:
    if not value:
        return None
    headers = _feishu_auth_headers()
    if not headers:
        return None
    try:
        resp = requests.post(
            _bitable_url("/search"),
            headers=headers,
            json={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {"field_name": field, "operator": "is", "value": [value]}
                    ],
                },
                "page_size": 1,
            },
            timeout=8,
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("Feishu search failed (%s=%s): %s", field, value, data)
            return None
        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            return None
        rec = items[0]
        raw_json = (rec.get("fields") or {}).get("order_json") or ""
        if isinstance(raw_json, list):
            raw_json = "".join(
                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in raw_json
            )
        if not raw_json:
            return None
        try:
            order = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(order, dict):
            return None
        return rec.get("record_id", ""), order
    except Exception as e:
        logger.warning("Feishu search error (%s=%s): %s", field, value, e)
        return None


# ---------------------------------------------------------------------------
# Order store helpers
# ---------------------------------------------------------------------------
def save_order(
    session_id: str,
    refer: str,
    total_price: float,
    items: list,
    checkout_url: str = "",
    cart_id: str = "",
    shopify_order_id: str = "",
    status: str = "pending",
) -> dict:
    """Store an order in memory + Feishu."""
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "session_id": session_id,
        "refer": refer,
        "total_price": total_price,
        "items": items,
        "checkout_url": checkout_url,
        "cart_id": cart_id,
        "shopify_order_id": shopify_order_id,
        "status": status,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    _orders[session_id] = record

    try:
        record_id = _feishu_create(record)
        if record_id:
            _record_ids[session_id] = record_id
    except Exception as e:
        logger.warning("save_order Feishu write failed: %s", e)

    return record


def get_order(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    if session_id in _orders:
        return _orders[session_id]
    try:
        found = _feishu_search("session_id", session_id)
    except Exception as e:
        logger.warning("get_order Feishu read failed: %s", e)
        return None
    if not found:
        return None
    record_id, order = found
    _orders[session_id] = order
    if record_id:
        _record_ids[session_id] = record_id
    return order


def update_order(session_id: str, **kwargs) -> Optional[dict]:
    if not session_id:
        return None
    record = _orders.get(session_id)
    if not record:
        fetched = get_order(session_id)
        if not fetched:
            return None
        record = fetched

    record.update(kwargs)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _orders[session_id] = record

    record_id = _record_ids.get(session_id)
    try:
        if record_id:
            _feishu_update(record_id, record)
        else:
            new_id = _feishu_create(record)
            if new_id:
                _record_ids[session_id] = new_id
    except Exception as e:
        logger.warning("update_order Feishu write failed: %s", e)

    return record


def find_by_shopify_order(shopify_order_id: str) -> Optional[dict]:
    if not shopify_order_id:
        return None
    for record in _orders.values():
        if record.get("shopify_order_id") == shopify_order_id:
            return record
    try:
        found = _feishu_search("shopify_order_id", shopify_order_id)
    except Exception as e:
        logger.warning("find_by_shopify_order Feishu read failed: %s", e)
        return None
    if not found:
        return None
    record_id, order = found
    _orders[order.get("session_id", "")] = order
    if record_id:
        _record_ids[order.get("session_id", "")] = record_id
    return order


def find_by_refer(refer: str) -> Optional[dict]:
    if not refer:
        return None
    for record in _orders.values():
        if record.get("refer") == refer:
            return record
    try:
        found = _feishu_search("refer", refer)
    except Exception as e:
        logger.warning("find_by_refer Feishu read failed: %s", e)
        return None
    if not found:
        return None
    record_id, order = found
    _orders[order.get("session_id", "")] = order
    if record_id:
        _record_ids[order.get("session_id", "")] = record_id
    return order


def generate_refer() -> str:
    chars = string.ascii_lowercase + string.digits
    return "ref_" + "".join(random.choices(chars, k=10))


# ---------------------------------------------------------------------------
# API Key validation
# ---------------------------------------------------------------------------
async def validate_api_key(request: Request):
    if request.url.path == "/api/health":
        return
    expected = API_AUTH_KEY
    if not expected:
        return
    # Check both header and query param
    provided = request.headers.get("X-Api-Key", "")
    if not provided:
        provided = request.query_params.get("key", "")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Warm-up endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    storefront_ok = bool(STOREFRONT_TOKEN)
    return {
        "status": "ok",
        "system": "a-station-local",
        "version": "2.2.0",
        "shopify_token": storefront_ok,
    }


@app.get("/api/health")
async def api_health():
    return await health()


# ---------------------------------------------------------------------------
# Config API (from config_api.py — Feishu 配置表)
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def api_config():
    """Return config from Feishu 配置表."""
    cfg = await get_config()
    return cfg


# ---------------------------------------------------------------------------
# Checkout endpoints
# ---------------------------------------------------------------------------
@app.post("/api/create_checkout")
async def create_checkout(req: CreateCheckoutRequest, raw_request: Request):
    """Create an order and Shopify checkout (JSON POST)."""
    await validate_api_key(raw_request)

    session_id = req.session_id
    refer = req.refer or generate_refer()
    total_price = req.total_price

    logger.info(
        "Checkout request: session=%s refer=%s total=%.2f items=%d",
        session_id, refer, total_price, len(req.items),
    )

    # Resolve variant
    try:
        match = select_random_matched_variant(total_price)
    except Exception as e:
        logger.error("Variant selection failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Variant selection failed: {str(e)}")

    variant_gid = match["variant_gid"]
    qty = match["qty"]
    unit_price = match["price"] / qty

    price_match = calculate_price_match(total_price)
    discount_amount = price_match["discount"]

    # Create Shopify cart
    try:
        cart = await create_checkout_cart(
            variant_gid=variant_gid,
            order_id=session_id,
            quantity=qty,
            refer=refer,
        )
    except Exception as e:
        logger.error("Shopify cart creation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Checkout creation failed: {str(e)}")

    checkout_url = cart.get("checkoutUrl")
    cart_id = cart.get("id")

    if not checkout_url:
        raise HTTPException(status_code=502, detail="No checkout URL returned from Shopify")

    # Store order
    save_order(
        session_id=session_id,
        refer=refer,
        total_price=total_price,
        items=req.items,
        checkout_url=checkout_url,
        cart_id=cart_id,
    )

    logger.info(
        "Checkout created: session=%s refer=%s url=%s qty=%d variant=%s",
        session_id, refer, checkout_url, qty, match["variant_title"],
    )

    return {
        "success": True,
        "session_id": session_id,
        "refer": refer,
        "checkout_url": checkout_url,
        "variant_title": match["variant_title"],
        "variant_gid": variant_gid,
        "unit_quantity": qty,
        "unit_price": unit_price,
        "total_price": match["price"],
        "discount_amount": discount_amount,
        "cart_id": cart_id,
    }


@app.post("/api/checkout-form")
async def checkout_form(
    session_id: str = Form(...),
    total_price: float = Form(...),
    items: str = Form("[]"),
    refer: str = Form(""),
    key: str = Form("apk_b9a7c3d1e5f80"),
):
    """Form-encoded checkout endpoint (A站 form POST → 302 redirect)."""
    if key != API_AUTH_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    try:
        parsed_items = json.loads(items) if items else []
        if not isinstance(parsed_items, list):
            parsed_items = []
    except json.JSONDecodeError:
        parsed_items = []

    refer = refer or generate_refer()

    logger.info(
        "Checkout-form request: session=%s refer=%s total=%.2f items=%d",
        session_id, refer, total_price, len(parsed_items),
    )

    try:
        match = select_random_matched_variant(total_price)
    except Exception as e:
        logger.error("Variant selection failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Variant selection failed: {str(e)}")

    variant_gid = match["variant_gid"]
    qty = match["qty"]

    calculate_price_match(total_price)

    try:
        cart = await create_checkout_cart(
            variant_gid=variant_gid,
            order_id=session_id,
            quantity=qty,
            refer=refer,
        )
    except Exception as e:
        logger.error("Shopify cart creation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Checkout creation failed: {str(e)}")

    checkout_url = cart.get("checkoutUrl")
    cart_id = cart.get("id")

    if not checkout_url:
        raise HTTPException(status_code=502, detail="No checkout URL returned from Shopify")

    try:
        save_order(
            session_id=session_id,
            refer=refer,
            total_price=total_price,
            items=parsed_items,
            checkout_url=checkout_url,
            cart_id=cart_id,
        )
    except Exception as e:
        logger.warning("save_order failed (non-fatal): %s", e)

    logger.info(
        "Checkout-form created: session=%s refer=%s url=%s qty=%d variant=%s",
        session_id, refer, checkout_url, qty, match["variant_title"],
    )

    return RedirectResponse(url=checkout_url, status_code=302)


# ---------------------------------------------------------------------------
# Order retrieval endpoints
# ---------------------------------------------------------------------------
@app.get("/api/order/{session_id}")
async def get_order_api(session_id: str):
    order = get_order(session_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.get("/api/order/by-refer/{refer}")
async def get_order_by_refer(refer: str):
    order = find_by_refer(refer)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found by refer")
    return order


# ---------------------------------------------------------------------------
# Shopify webhook endpoint
# ---------------------------------------------------------------------------
@app.post("/api/webhook/shopify")
async def shopify_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Shopify-Hmac-Sha256", "")
    topic = request.headers.get("X-Shopify-Topic", "")

    if not verify_webhook_hmac(body, signature):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

    logger.info("Received Shopify webhook: topic=%s", topic)

    data = json.loads(body)

    if topic == "orders/create":
        shopify_order_id = str(data.get("id", ""))
        note = data.get("note", "")
        order_number = data.get("order_number", "")

        logger.info(
            "Shopify order #%s created: id=%s note=%s",
            order_number, shopify_order_id, note,
        )

        session_id = None
        refer = None

        if note:
            ref_match = re.search(r"Refer:\s*(\S+)", note)
            if ref_match:
                refer = ref_match.group(1)
            sid_match = re.search(r"Order Reference:\s*(\S+)", note)
            if sid_match:
                session_id = sid_match.group(1)

        if session_id:
            update_order(session_id, status="paid", shopify_order_id=shopify_order_id)
            logger.info(
                "Order %s updated to 'paid' (Shopify Order #%s, refer=%s)",
                session_id, order_number, refer,
            )

    elif topic == "orders/fulfilled":
        shopify_order_id = str(data.get("id", ""))
        order = find_by_shopify_order(shopify_order_id)
        if order:
            update_order(order["session_id"], status="fulfilled")
            logger.info("Order %s updated to 'fulfilled'", order["session_id"])

    return {"status": "received"}


# ---------------------------------------------------------------------------
# Debug: list all orders
# ---------------------------------------------------------------------------
@app.get("/api/orders")
async def list_orders():
    return {"orders": list(_orders.values()), "count": len(_orders)}
