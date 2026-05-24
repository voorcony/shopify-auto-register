#!/usr/bin/env bash
# =============================================================================
# status-monitor.sh — Three-Layer Station Service Monitor
# =============================================================================
# Runs every 5 minutes via crontab.
# Checks each service, writes status to Feishu 状态表.
# Logs to /var/log/station-monitor.log
# =============================================================================

LOG_FILE="/var/log/station-monitor.log"
NOW_TS=$(date +%s)
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# ---- Feishu Config ----
FEISHU_APP_ID="cli_a9619830e2fadcd1"
FEISHU_APP_SECRET="kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
BASE_TOKEN="XKOCbEsKpaGRQgsrLB3c5vkFn2b"
ST_TABLE_ID="tbl2dC0F2nX8fonn"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

get_feishu_token() {
  local resp
  resp=$(curl -s --max-time 10 \
    -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\":\"$FEISHU_APP_ID\",\"app_secret\":\"$FEISHU_APP_SECRET\"}")
  echo "$resp" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get('tenant_access_token',''))
except:
    pass
" 2>/dev/null
}

get_record_id_by_service() {
  local service="$1" token="$2"
  local url="https://open.feishu.cn/open-apis/bitable/v1/apps/${BASE_TOKEN}/tables/${ST_TABLE_ID}/records?page_size=50"
  local resp
  resp=$(curl -s --max-time 10 \
    -H "Authorization: Bearer $token" \
    "$url")
  echo "$resp" | python3 -c "
import sys,json
try:
    data=json.load(sys.stdin)
    for item in data.get('data',{}).get('items',[]):
        f=item.get('fields',{})
        if f.get('service','') == '$service':
            print(item.get('record_id',''))
            break
except:
    pass
" 2>/dev/null
}

update_status() {
  local service="$1" status="$2" rt="$3" error="$4" token="$5"
  local payload record_id

  record_id=$(get_record_id_by_service "$service" "$token")

  payload=$(python3 -c "
import json
d = {
    'fields': {
        'service': '$service',
        'status': '$status',
        'response_time_ms': $rt,
        'last_checked': $NOW_TS,
        'error': '$error'
    }
}
print(json.dumps(d))
")

  if [ -n "$record_id" ]; then
    curl -s --max-time 10 \
      -X PUT "https://open.feishu.cn/open-apis/bitable/v1/apps/${BASE_TOKEN}/tables/${ST_TABLE_ID}/records/${record_id}" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "$payload" > /dev/null 2>&1
    log "UPDATED $service → $status (${rt}ms)"
  else
    curl -s --max-time 10 \
      -X POST "https://open.feishu.cn/open-apis/bitable/v1/apps/${BASE_TOKEN}/tables/${ST_TABLE_ID}/records" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      -d "$payload" > /dev/null 2>&1
    log "CREATED $service → $status (${rt}ms)"
  fi
}

# ---- Main ----
log "=== Monitor run starting ==="

# Get Feishu token
FEISHU_TOKEN=$(get_feishu_token)
if [ -z "$FEISHU_TOKEN" ]; then
  log "ERROR: Failed to get Feishu token"
  exit 1
fi

# Check A站
log "Checking A站..."
A_START=$(date +%s%N)
A_CODE=$(curl -sI --max-time 10 -o /dev/null -w "%{http_code}" \
  -H "Host: luxury.superfiremarket.com" http://localhost:80/ 2>/dev/null || echo "000")
A_END=$(date +%s%N)
A_RT=$(( (A_END - A_START) / 1000000 ))
if [ "$A_CODE" = "200" ] || [ "$A_CODE" = "302" ]; then
  update_status "a-station" "online" "$A_RT" "" "$FEISHU_TOKEN"
else
  update_status "a-station" "offline" "$A_RT" "HTTP $A_CODE" "$FEISHU_TOKEN"
fi

# Check B站
log "Checking B站..."
B_START=$(date +%s%N)
B_RESP=$(curl -s --max-time 10 \
  "https://coolingsheet.shop/api/health" 2>/dev/null || echo "{}")
B_END=$(date +%s%N)
B_RT=$(( (B_END - B_START) / 1000000 ))
B_STATUS=$(echo "$B_RESP" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get('status','offline'))
except:
    print('offline')
" 2>/dev/null || echo "offline")
if [ "$B_STATUS" = "ok" ]; then
  update_status "b-station" "online" "$B_RT" "" "$FEISHU_TOKEN"
else
  update_status "b-station" "offline" "$B_RT" "Status: $B_STATUS" "$FEISHU_TOKEN"
fi

# Check Shopify
log "Checking Shopify..."
S_START=$(date +%s%N)
S_CODE=$(curl -sI --max-time 10 -o /dev/null -w "%{http_code}" \
  "https://147xvt-jc.myshopify.com/" 2>/dev/null || echo "000")
S_END=$(date +%s%N)
S_RT=$(( (S_END - S_START) / 1000000 ))
if [ "$S_CODE" = "200" ] || [ "$S_CODE" = "301" ] || [ "$S_CODE" = "302" ]; then
  update_status "shopify" "online" "$S_RT" "" "$FEISHU_TOKEN"
else
  update_status "shopify" "offline" "$S_RT" "HTTP $S_CODE" "$FEISHU_TOKEN"
fi

# Check Feishu
log "Checking Feishu..."
F_START=$(date +%s%N)
F_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
  -H "Content-Type: application/json" \
  -d "{\"app_id\":\"$FEISHU_APP_ID\",\"app_secret\":\"$FEISHU_APP_SECRET\"}" 2>/dev/null || echo "000")
F_END=$(date +%s%N)
F_RT=$(( (F_END - F_START) / 1000000 ))
if [ "$F_CODE" = "200" ]; then
  update_status "feishu" "online" "$F_RT" "" "$FEISHU_TOKEN"
else
  update_status "feishu" "offline" "$F_RT" "HTTP $F_CODE" "$FEISHU_TOKEN"
fi

log "=== Monitor run complete ==="
