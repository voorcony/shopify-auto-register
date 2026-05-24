#!/usr/bin/env python3
"""CAPI事件服务器 - 接收前端事件并转发到Meta Conversions API"""
import os, json, hashlib, hmac, logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

app = FastAPI(title="Bag Store CAPI Server")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Meta CAPI配置 (从环境变量读取)
META_PIXEL_ID = os.getenv("META_PIXEL_ID", "YOUR_PIXEL_ID")
META_CAPI_TOKEN = os.getenv("META_CAPI_TOKEN", "YOUR_CAPI_TOKEN")
META_CAPI_URL = f"https://graph.facebook.com/v22.0/{META_PIXEL_ID}/events"
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("capi")

def send_capi_event(event_name: str, event_data: dict, user_data: dict = None):
    """发送事件到Meta Conversions API"""
    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(__import__('time').time()),
            "action_source": "website",
            "event_source_url": event_data.get("source_url", ""),
            "user_data": user_data or {
                "client_ip_address": event_data.get("ip", ""),
                "client_user_agent": event_data.get("ua", ""),
                "fbc": event_data.get("fbc", ""),
                "fbp": event_data.get("fbp", ""),
            },
            "custom_data": {k: v for k, v in event_data.items() 
                          if k not in ("ip", "ua", "fbc", "fbp", "source_url")}
        }],
        "access_token": META_CAPI_TOKEN
    }
    
    try:
        resp = httpx.post(META_CAPI_URL, json=payload, timeout=10)
        result = resp.json()
        logger.info(f"CAPI {event_name}: {resp.status_code} - {result.get('events_received', 0)} events")
        return result
    except Exception as e:
        logger.error(f"CAPI {event_name} error: {e}")
        return {"error": str(e)}

@app.get("/health")
def health():
    return {"status": "ok", "pixel_id": META_PIXEL_ID[:6] + "..." if META_PIXEL_ID != "YOUR_PIXEL_ID" else "not configured"}

@app.post("/api/capi/event")
async def receive_event(request: Request):
    """接收来自A站的前端事件, 转发到Meta CAPI"""
    body = await request.json()
    event_name = body.get("event_name", "")
    event_data = body.get("event_data", {})
    
    # 添加客户端信息
    event_data["ip"] = request.client.host
    event_data["ua"] = request.headers.get("user-agent", "")
    
    result = send_capi_event(event_name, event_data)
    return {"status": "ok", "result": result}

@app.post("/api/capi/purchase")
async def purchase_webhook(request: Request):
    """接收Shopify orders/create webhook, 发送Purchase事件到Meta CAPI"""
    body = await request.body()
    headers = dict(request.headers)
    
    # 验证Shopify webhook HMAC
    if SHOPIFY_WEBHOOK_SECRET:
        received_hmac = headers.get("x-shopify-hmac-sha256", "")
        expected_hmac = base64.b64encode(
            hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
        ).decode()
        if received_hmac != expected_hmac:
            logger.warning("Invalid Shopify webhook HMAC")
            raise HTTPException(401, "Invalid HMAC")
    
    data = json.loads(body)
    topic = headers.get("x-shopify-topic", "")
    
    if topic == "orders/create":
        total_price = float(data.get("total_price", 0))
        currency = data.get("currency", "USD")
        email = data.get("email", "")
        phone = data.get("phone", "")
        order_id = data.get("order_number", "")
        
        # 构建用户数据(用于匹配)
        user_data = {"em": [hashlib.sha256(email.encode()).hexdigest()] if email else []}
        if phone:
            user_data["ph"] = [hashlib.sha256(phone.encode()).hexdigest()]
        
        # 发送Purchase事件
        send_capi_event("Purchase", {
            "value": total_price,
            "currency": currency,
            "order_id": str(order_id),
        }, user_data)
        
        logger.info(f"Purchase event sent: Order #{order_id}, ${total_price}")
    
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100)
