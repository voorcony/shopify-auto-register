import re

with open('/home/ubuntu/landing-backend/checkout_api.py', 'r') as f:
    content = f.read()

insertion_marker = '# ---------------------------------------------------------------------------\n# Checkout endpoints\n# ---------------------------------------------------------------------------'
new_endpoint = '''# ---------------------------------------------------------------------------
# Checkout endpoints
# ---------------------------------------------------------------------------
@app.post("/api/onewpay-checkout")
async def onewpay_checkout(req: OnewpayCheckoutRequest):
    """Unified order API: record in Feishu, get onewpay checkout URL, return to caller."""
    import uuid as uuid_mod
    import requests as sync_requests

    session_id = req.session_id or str(uuid_mod.uuid4())
    refer = req.refer or session_id
    source = req.source or "unknown"
    items = req.items or []
    amount = req.amount

    logger.info(
        "onewpay-checkout: source=%s session=%s refer=%s amount=%.2f items=%d",
        source, session_id, refer, amount, len(items),
    )

    # 1. Parse items summary
    item_count, total_qty, brand_str, summary_str = _parse_items_summary(items)

    # 2. Create Feishu record
    feishu_body = {
        "fields": {
            "session_id": session_id,
            "refer": refer,
            "source": source,
            "status": "pending",
            "order_json": json.dumps(req.model_dump(), ensure_ascii=False, default=str),
            "商品数量": item_count,
            "总件数": total_qty,
            "品牌": brand_str,
            "商品摘要": summary_str,
        }
    }

    record_id = None
    checkout_url = None
    order_id = None

    try:
        headers = _feishu_auth_headers()
        if headers:
            resp = sync_requests.post(
                _bitable_url(),
                headers=headers,
                json=feishu_body,
                timeout=8,
            )
            data = resp.json()
            if data.get("code") == 0:
                record_id = ((data.get("data") or {}).get("record") or {}).get("record_id")
                logger.info("Feishu record created: %s", record_id)
            else:
                logger.warning("Feishu create failed: %s", data)
    except Exception as e:
        logger.warning("Feishu create error: %s", e)

    # 3. Call onewpay API
    try:
        # Build product_name summary
        if items and isinstance(items, list):
            product_name = "; ".join(
                '{q}x {n}'.format(
                    q=item.get("quantity", 1),
                    n=item.get("name", item.get("product_name", "Item"))
                )
                for item in items
            )
        else:
            product_name = "Order " + session_id[:8]

        onewpay_resp = sync_requests.post(
            "https://dashborad.onewpay.com/api/public/checkout",
            json={
                "api_key": "solevora-secret-2025",
                "product_name": product_name,
                "amount": amount,
            },
            timeout=15,
        )
        onewpay_data = onewpay_resp.json()
        checkout_url = onewpay_data.get("checkout_url")
        order_id = onewpay_data.get("order_id", str(onewpay_data.get("id", "")))
        logger.info("onewpay response: checkout_url=%s order_id=%s", checkout_url, order_id)
    except Exception as e:
        logger.error("onewpay API call failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"error": "onewpay API call failed: " + str(e)},
        )

    if not checkout_url:
        logger.error("onewpay returned no checkout_url: %s", onewpay_data)
        return JSONResponse(
            status_code=502,
            content={"error": "onewpay returned no checkout URL"},
        )

    # 4. Update Feishu record with checkout URL and order ID
    if record_id:
        try:
            headers = _feishu_auth_headers()
            if headers:
                resp = sync_requests.put(
                    _bitable_url("/" + record_id),
                    headers=headers,
                    json={"fields": {
                        "checkout_url": checkout_url,
                        "shopify_order_id": order_id,
                        "source": source,
                        "status": "pending",
                    }},
                    timeout=8,
                )
                logger.info("Feishu record updated: %s", resp.json())
        except Exception as e:
            logger.warning("Feishu update error: %s", e)

    return {
        "checkout_url": checkout_url,
        "record_id": record_id or "",
        "session_id": session_id,
        "order_id": order_id,
    }


'''

if insertion_marker not in content:
    print("ERROR: Could not find insertion marker")
    print("Looking for:", repr(insertion_marker[:50]))
    print("Found in content:", insertion_marker in content)
    exit(1)

content = content.replace(insertion_marker, new_endpoint, 1)

with open('/home/ubuntu/landing-backend/checkout_api.py', 'w') as f:
    f.write(content)

print('Endpoint added successfully')
