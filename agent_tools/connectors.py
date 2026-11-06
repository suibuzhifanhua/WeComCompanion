"""agent_tools.connectors — 连接器定义与 HTTP 执行（CONNECTORS / is_connector_configured / run_connector）。"""

# v1.0.324 启动提速 + v1.0.364 M8 收敛：requests 懒加载代理实现收敛到 core_utils
from core_utils import _lazy_requests as requests
import json

from core_utils import (log_event)

# 连接器：通用 HTTP 工具（配置后可让 agent 访问外部 API）
CONNECTORS = [
    {"id": "generic-http", "name": "通用 HTTP 工具", "icon": "🌐",
     "desc": "配置外部 API 的 Base URL 与密钥后，agent 即可调用 generic_http_call 访问该服务（内部 OA、SaaS、REST API 等）。",
     "prompt": "你已接入外部 HTTP 服务（generic-http）。需要调用它时，用 generic_http_call 指定 method/path/body；凭据由系统自动附带，无需手动拼接。",
     "config_schema": [
         {"key": "base_url", "label": "API Base URL", "type": "text",
          "placeholder": "https://api.example.com", "required": True},
         {"key": "api_key", "label": "API Key / Token", "type": "password",
          "placeholder": "可选，Bearer 自动附带", "required": False},
         {"key": "headers", "label": "额外请求头 (JSON)", "type": "textarea",
          "placeholder": '{"X-Tenant":"abc"}', "required": False},
     ],
     "tool": {"type": "function", "function": {
         "name": "generic_http_call",
         "description": "调用已配置的外部 HTTP 服务（generic-http 连接器）。系统会自动附带配置的 API Key 与额外请求头。",
         "parameters": {"type": "object", "properties": {
             "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP 方法，默认 GET"},
             "path": {"type": "string", "description": "接口路径，如 /v1/users 或 /orders/123"},
             "params": {"type": "object", "description": "GET 查询参数 (key->value)"},
             "body": {"type": "object", "description": "POST/PUT/PATCH 的请求体(JSON)"},
         }, "required": ["path"]}
     }}},
]

def is_connector_configured(connector, cc):
    cc = cc or {}
    for f in connector.get("config_schema", []):
        if f.get("required") and not str(cc.get(f["key"], "")).strip():
            return False
    return True

def run_connector(connector, cc, args):
    if connector["id"] == "generic-http":
        method = (args.get("method") or "GET").upper()
        path = args.get("path", "") or ""
        base = (cc.get("base_url") or "").rstrip("/")
        url = base + (path if path.startswith("/") else "/" + path)
        headers = {}
        if cc.get("api_key"):
            headers["Authorization"] = "Bearer " + str(cc["api_key"])
        try:
            extra = json.loads(cc.get("headers") or "{}")
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
        except Exception as e:  # noqa: BLE001
            log_event(f"[connector] 连接器自定义 headers 解析失败，已忽略：{e}")
        try:
            body = args.get("body") if method in ("POST", "PUT", "PATCH") else None
            params = args.get("params") if method == "GET" else None
            r = requests.request(method, url, headers=headers, json=body, params=params, timeout=30)
            try:
                text = r.text or ""
            except Exception as e:  # noqa: BLE001
                log_event(f"[http] 读取响应正文失败：{e}")
                text = ""
            return {"status": r.status_code, "body": text[:6000]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP 调用失败：{e}"}
    return {"ok": False, "error": f"未知连接器：{connector.get('id')}"}
