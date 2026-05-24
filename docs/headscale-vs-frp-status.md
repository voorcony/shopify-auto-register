# 隧道状态 - 2026-05-18 15:59

## 当前方案
✅ **SSH 反向隧道**（方案C）已建立

## 架构
- Windows PC (BU-30b:11434) 
  → SSH tunnel (-R 11434:localhost:11434) 
  → 小助理 localhost:11434

## 验证结果
- ✅ SSH 隧道连接成功（TCP 握手正常）
- ⚠️ BU-30b 尚未响应模型列表（可能还在加载中）
- 连接测试: curl http://127.0.0.1:11434/v1/models → Empty reply

## 未完成事项
1. 等待 BU-30b 在 Windows 上完全加载
2. 配置 Hermes 小助理的 LLM 指向 http://127.0.0.1:11434/v1
3. 验证模型 API 正常工作

## 废弃方案
- ❌ 方案A (Headscale): TLS/Windows 服务问题导致放弃
- ❌ 方案B (frp): 运营商拦截/frpc 握手超时
