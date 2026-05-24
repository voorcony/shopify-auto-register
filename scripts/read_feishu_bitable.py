#!/usr/bin/env python3
"""Read all product data from Feishu bitable shopify_products_rest_com"""
import requests
import json
import time

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
APP_TOKEN = "JSfNsNXXJhFmoHtWsGHckJFrnNh"

def get_tenant_token():
    """Step 1: Get tenant access token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Failed to get token: {data}")
    print(f"[OK] Got tenant token: {data.get('tenant_access_token')[:20]}...")
    return data["tenant_access_token"]

def get_table_id(token):
    """Step 2: Get table info from bitable"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    data = resp.json()
    print(f"[DEBUG] Tables API response code: {data.get('code')}")
    if data.get("code") != 0:
        print(f"[ERROR] Tables API error: {json.dumps(data, indent=2, ensure_ascii=False)}")
        raise Exception(f"Failed to get tables: {data}")
    tables = data.get("data", {}).get("items", [])
    print(f"[OK] Found {len(tables)} table(s) in bitable")
    for t in tables:
        print(f"  - Table: name='{t.get('name')}', id='{t.get('table_id')}'")
    # Find the table named shopify_products_rest_com
    target = None
    for t in tables:
        if t.get("name") == "shopify_products_rest_com":
            target = t
            break
    if not target and tables:
        target = tables[0]
        print(f"[WARN] Table 'shopify_products_rest_com' not found by exact name, using first table: {target.get('name')}")
    if not target:
        raise Exception("No tables found in bitable")
    return target["table_id"], target.get("name")

def get_fields(token, table_id):
    """Step 3: Get all fields (columns) of the table"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    data = resp.json()
    if data.get("code") != 0:
        print(f"[ERROR] Fields API error: {json.dumps(data, indent=2, ensure_ascii=False)}")
        raise Exception(f"Failed to get fields: {data}")
    fields = data.get("data", {}).get("items", [])
    print(f"[OK] Found {len(fields)} field(s):")
    for f in fields:
        print(f"  - field_id='{f.get('field_id')}', name='{f.get('field_name')}', type={f.get('type')}")
    return fields

def get_all_records(token, table_id):
    """Step 4: Get all records (paginated)"""
    headers = {"Authorization": f"Bearer {token}"}
    all_records = []
    page_token = None
    page_num = 0
    
    while True:
        page_num += 1
        params = {"page_size": 20}
        if page_token:
            params["page_token"] = page_token
        
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records"
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()
        
        if data.get("code") != 0:
            print(f"[ERROR] Records API error: {json.dumps(data, indent=2, ensure_ascii=False)}")
            raise Exception(f"Failed to get records: {data}")
        
        records = data.get("data", {}).get("items", [])
        all_records.extend(records)
        print(f"[OK] Page {page_num}: got {len(records)} records (total: {len(all_records)})")
        
        has_more = data.get("data", {}).get("has_more", False)
        page_token = data.get("data", {}).get("page_token")
        
        if not has_more or not page_token:
            break
    
    return all_records

def main():
    print("=" * 60)
    print("Feishu Bitable Reader - shopify_products_rest_com")
    print("=" * 60)
    
    # Step 1: Get token
    token = get_tenant_token()
    
    # Step 2: Get table_id
    table_id, table_name = get_table_id(token)
    print(f"\n[INFO] Using table: '{table_name}' (id: {table_id})")
    
    # Step 3: Get fields
    print("\n" + "=" * 60)
    print("FIELDS (COLUMNS)")
    print("=" * 60)
    fields = get_fields(token, table_id)
    
    # Step 4: Get all records
    print("\n" + "=" * 60)
    print("RECORDS")
    print("=" * 60)
    records = get_all_records(token, table_id)
    
    print(f"\n[SUMMARY] Total records: {len(records)}")
    
    if records:
        # Print field names as header
        field_names = [f.get("field_name") for f in fields]
        print(f"\n[FIELDS] {', '.join(field_names)}")
        
        print("\n" + "-" * 60)
        for i, rec in enumerate(records):
            fields_data = rec.get("fields", {})
            record_id = rec.get("record_id", "N/A")
            print(f"\n--- Record {i+1} (id: {record_id}) ---")
            # Print fields that look like product data
            for f in fields:
                fname = f.get("field_name")
                ftype = f.get("type")
                val = fields_data.get(fname, fields_data.get(f.get("field_id"), "N/A"))
                # Skip empty fields for cleaner output
                if val and val not in (None, "", [], {}, "N/A"):
                    # Truncate long values
                    val_str = json.dumps(val, ensure_ascii=False, default=str)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    print(f"  {fname}: {val_str}")
                else:
                    print(f"  {fname}: (empty)")
        
        print("\n" + "-" * 60)
        
        # Summary of product names
        print("\n[PRODUCT NAMES SUMMARY]")
        for i, rec in enumerate(records):
            fields_data = rec.get("fields", {})
            name = fields_data.get("标题", fields_data.get("product_name", 
                   fields_data.get("Product Name", fields_data.get("name", 
                   fields_data.get("产品名称", "N/A")))))
            print(f"  {i+1}. {name}")
    else:
        print("[WARN] No records found in the table.")

if __name__ == "__main__":
    main()
