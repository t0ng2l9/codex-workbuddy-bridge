#!/usr/bin/env python3
"""WorkBuddy AI → OpenAI 兼容本地代理。

把 WorkBuddy AI 桌面端的登录态变成任何 OpenAI 兼容客户端可用的
`/v1/chat/completions`。零外部依赖（仅 stdlib + cryptography）。

上游要点（实测得出，非推测）：
  - 凭据在 %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop-ai.info
    accessToken/refreshToken 是 AES-256-GCM 信封；密钥要跑 WorkBuddy 自己的
    Electron 二进制（ELECTRON_RUN_AS_NODE=1）取 workbuddyStorage 绑定。
  - 对话端点 https://www.workbuddy.ai/v2/chat/completions **必须 stream=true**
    （否则 400 code 11101），且**首条消息必须是 system**（否则 400 code 11128）。
  - 模型目录 GET /v3/config，UA 必须带 WorkBuddyAI/<版本>。
  - 额度 POST /v2/billing/meter/get-user-resource。
  - 响应里 usage.credit 就是本次实际消耗的积分。

用法：
    python wbproxy.py [--port 8788] [--host 127.0.0.1] [--verbose]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import wbdiscover
import wbresponses

# ─────────────────────────── 配置 ───────────────────────────

GLOBAL_BASE = "https://www.workbuddy.ai"

# 桌面端路径与凭据文件一律自动发现（见 wbdiscover），不写死盘符/用户名。
# 环境变量 WBBRIDGE_APP / WBBRIDGE_AUTH_FILE 可覆盖。
APP_DIR = wbdiscover.find_app() or ""
AUTH_FILE = wbdiscover.find_auth_file() or ""
HELPER_JS = (
    'process.stdout.write(String(process._linkedBinding('
    '"electron_browser_workbuddy_storage").loggerGet()))'
)
DEFAULT_KEY = "wbbridge-local"          # 客户端随便填，本地代理不校验
TOKEN_SKEW_SEC = 120                     # 提前 2 分钟视为过期

_VERBOSE = False

# 实际生效的模型追踪：代理是唯一看到真实 model 字段的地方，
# /wb/last-model 据此回答「当前到底在用哪个模型」。
LAST_MODEL: dict = {"model": None, "ts": 0, "count": 0}

# ── 模型同步：桌面端在选择器里切换模型时只作用于当前会话，不会写回
# config.toml，导致「配置默认」与「实际在用」长期脱节（读配置的人会报错模型）。
# 代理既然每次都看到真实 model，就顺手把顶层 model 同步回配置，让两处一致。
CODEX_CONFIG = os.environ.get(
    "WBBRIDGE_CODEX_CONFIG",
    os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
)
SYNC_CONFIG = os.environ.get("WBBRIDGE_SYNC_CONFIG", "1") not in ("0", "false", "no")
_SYNC_LOCK = threading.Lock()
_CONFIG_MODEL: str | None = None          # 已知的配置里写的模型，避免重复读写


def _config_model() -> str | None:
    """读 config.toml 里第一个顶层 `model = ...`（遇到 [section] 即停）。"""
    try:
        # newline="" 保留原换行符；文本模式读取会把 CRLF 折成 LF，
        # 之后写回就整份文件被改写（diff 里「整个文件都变了」）。
        with open(CODEX_CONFIG, encoding="utf-8", newline="") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("["):
                    break
                if s.startswith("model ") or s.startswith("model="):
                    if "=" in s:
                        return s.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _sync_config_model(model: str) -> None:
    """把顶层 model 改成实际在用的模型；只动那一行，其余原样保留。"""
    global _CONFIG_MODEL
    if not SYNC_CONFIG or not model:
        return
    with _SYNC_LOCK:
        if _CONFIG_MODEL is None:
            _CONFIG_MODEL = _config_model()
        if _CONFIG_MODEL == model:
            return
        try:
            with open(CODEX_CONFIG, encoding="utf-8", newline="") as fh:
                lines = fh.readlines()
        except OSError as e:
            log(f"同步模型失败（读不到配置）: {e}")
            return
        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("["):
                break                     # 只改顶层键，不碰任何 [section]
            if s.startswith("model ") or s.startswith("model="):
                if "=" not in s:
                    continue
                old = s.split("=", 1)[1].strip()
                if old.strip('"').strip("'") == model:
                    _CONFIG_MODEL = model
                    return
                nl = "\r\n" if line.endswith("\r\n") else "\n"
                lines[i] = f'model = "{model}"{nl}'
                changed = True
                break
        if not changed:
            log(f"同步模型：配置里没有顶层 model 键，跳过")
            return
        try:
            tmp = CODEX_CONFIG + ".wbbridge-tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            os.replace(tmp, CODEX_CONFIG)
            _CONFIG_MODEL = model
            log(f"已同步配置默认模型 → {model}")
        except OSError as e:
            log(f"同步模型失败（写不了配置）: {e}")


def _note_model(model: str) -> None:
    LAST_MODEL["model"] = model
    LAST_MODEL["ts"] = int(time.time())
    LAST_MODEL["count"] = LAST_MODEL.get("count", 0) + 1
    _sync_config_model(model)


def log(*a):
    """请求流水（默认静音，--verbose 打开）。"""
    if _VERBOSE:
        print("[wbproxy]", *a, file=sys.stderr, flush=True)


def upstream_error_text(detail) -> str:
    """把上游错误体压成一行可读摘要。

    直接按字数截断原始 JSON 会切掉最有用的中文处置提示（它排在一堆双语字段后面），
    所以这里解析出 code / msg / displayTips.zh 再拼一行；解析不了才退回截断。
    """
    if isinstance(detail, dict):
        o, s = detail, json.dumps(detail, ensure_ascii=False)
    else:
        s = detail if isinstance(detail, str) else str(detail)
        try:
            o = json.loads(s)
        except Exception:
            return s[:700]
        if not isinstance(o, dict):
            return s[:700]

    parts = []
    if o.get("code") is not None:
        parts.append(f"code={o['code']}")
    if o.get("msg"):
        parts.append(str(o["msg"]))

    tip = None
    for key in ("displayTips", "displayMsg"):
        v = o.get(key)
        if isinstance(v, dict):
            tip = v.get("zh") or v.get("en")
        elif isinstance(v, str):
            tip = v
        if tip:
            break

    # 上游有时把内层 JSON 再塞在 error.message 字符串里
    if not tip:
        err = o.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            try:
                return upstream_error_text(json.loads(err["message"]))
            except Exception:
                tip = err["message"]

    if tip:
        parts.append(tip)
    if o.get("requestId"):
        parts.append(f"requestId={o['requestId']}")
    return " / ".join(parts) if parts else s[:700]


def log_err(*a):
    """错误类事件，**不受 --verbose 门控**。

    上游报错（尤其 429 / code 14003）是用户唯一能看到原因的地方；如果只在 verbose
    模式下记录，别人拿到包遇到限流时日志里空空如也，只能来回问。
    """
    print("[wbproxy]", *a, file=sys.stderr, flush=True)


# ─────────────────────── 凭据：解密与刷新 ───────────────────────

def _aad(key_id: str, suite: int) -> bytes:
    """WorkBuddy 的 AAD 构造（照抄其 buildAuthenticatedContextAad）。"""
    def lp(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack(">I", len(b)) + b

    return (b"WB-AAD\x00" + b"\x01" + lp("WBEV1") + lp("sym-v1")
            + struct.pack(">I", suite) + lp(key_id) + b"\x02" + b"\x00" + b"\x00")


def _electron_payload() -> dict:
    """跑 WorkBuddy 的 Electron 取 at-rest 密钥。"""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    r = subprocess.run(
        [APP_DIR, "-e", HELPER_JS], capture_output=True, text=True, timeout=60, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f"WorkBuddy 密钥助手失败（exit={r.returncode}）；确认 App 已安装：{APP_DIR}"
        )
    return json.loads(r.stdout.strip())


def _decrypt_field(wrapped: dict, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    inner = json.loads(base64.b64decode(wrapped["envelope"]))
    if inner.get("suite") != 1:
        raise RuntimeError(f"未知信封 suite={inner.get('suite')}")
    pt = AESGCM(key).decrypt(
        base64.b64decode(inner["nonce"]),
        base64.b64decode(inner["ciphertext"]) + base64.b64decode(inner["authTag"]),
        _aad(inner["keyId"], inner["suite"]),
    )
    return pt.decode("utf-8")


class Credentials:
    """WorkBuddy AI 凭据：读桌面端 auth 文件 → 解密 → 到期自动刷新。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: bytes | None = None
        # 网关的渠道校验认「会话标识」，同一个代理进程复用一次即可。
        self.conversation_id = str(uuid.uuid4())
        self.access_token = ""
        self.refresh_token = ""
        self.uid = ""
        self.domain = ""
        self.expires_at = 0.0          # epoch 秒
        self.app_version = "5.5.2"
        self._load_app_version()
        self.reload()

    def _load_app_version(self) -> None:
        """UA 里要带 App 版本，网关按它分流 /v3/config。"""
        try:
            vpath = os.path.join(os.path.dirname(APP_DIR), "version")
            v = open(vpath, encoding="utf-8").read().strip()
            if v:
                self.app_version = v
        except OSError:
            pass
        log("app version:", self.app_version)

    def _protector_key(self) -> bytes:
        if self._key is None:
            payload = _electron_payload()
            if payload.get("version") != 1:
                raise RuntimeError("密钥 payload 版本不符")
            self._key = hashlib.sha256(payload["atRestSecretKey"].encode("utf-8")).digest()
        return self._key

    def reload(self) -> None:
        with self._lock:
            doc = json.loads(open(AUTH_FILE, encoding="utf-8").read())
            auth = doc.get("auth", {})
            acct = doc.get("account", {})
            key = self._protector_key()

            def read(name: str) -> str:
                v = auth.get(name)
                if isinstance(v, str):
                    return v
                if isinstance(v, dict) and v.get("$wbEncrypted") == 1:
                    return _decrypt_field(v, key)
                return ""

            self.access_token = read("accessToken")
            self.refresh_token = read("refreshToken")
            self.uid = acct.get("uid", "")
            self.domain = auth.get("domain", "") or "www.workbuddy.ai"
            # expiresAt 是毫秒
            raw_exp = auth.get("expiresAt")
            if isinstance(raw_exp, (int, float)) and raw_exp > 0:
                self.expires_at = raw_exp / 1000.0
            else:
                exp_in = auth.get("expiresIn") or 0
                self.expires_at = time.time() + float(exp_in)
            log(f"凭据已加载 uid={self.uid[:8]}… 到期 {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.expires_at))}")

    # ── 请求头 ──
    def _common(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": GLOBAL_BASE,
            "Referer": GLOBAL_BASE + "/",
            "User-Agent": f"WorkBuddyAI/{self.app_version}",
            "X-User-Id": self.uid,
            "X-No-Enterprise-Id": "1",
            "X-Domain": self.domain,
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Version": self.app_version,
            "X-Product": "SaaS",
        }

    def chat_headers(self) -> dict:
        h = self._common()
        h["Content-Type"] = "application/json"
        h["Authorization"] = "Bearer " + self.access_token
        # 渠道校验（400 code 11128）：官方客户端每次对话都会带会话/请求标识，
        # 缺这些头会被判定为「非官方客户端」直接拦截 —— 与 system prompt 无关。
        h["User-Agent"] = f"WorkBuddy/{self.app_version} WorkBuddy/{self.app_version}"
        h["X-Conversation-ID"] = self.conversation_id
        h["X-Conversation-Request-ID"] = str(uuid.uuid4())
        h["X-Conversation-Message-ID"] = str(uuid.uuid4())
        h["X-Request-ID"] = str(uuid.uuid4())
        h["x-codebuddy-request"] = "1"
        h["X-Agent-Intent"] = "craft"
        h["X-Agent-Purpose"] = "conversation"
        h["X-Private-Data"] = "true"
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        parent_id = uuid.uuid4().hex[:16]
        h["traceparent"] = f"00-{trace_id}-{span_id}-01"
        h["b3"] = f"{trace_id}-{span_id}-1-{parent_id}"
        h["X-B3-TraceId"] = trace_id
        h["X-B3-ParentSpanId"] = parent_id
        h["X-B3-SpanId"] = span_id
        h["X-B3-Sampled"] = "1"
        h["X-Trace-ID"] = trace_id
        return h

    def expired(self) -> bool:
        return time.time() >= (self.expires_at - TOKEN_SKEW_SEC)

    def ensure_fresh(self) -> None:
        if self.expired():
            self.refresh()

    def refresh(self) -> None:
        """POST /v2/plugin/auth/token/refresh（X-Refresh-Token 只出现在这里）。"""
        with self._lock:
            if not self.refresh_token:
                raise RuntimeError("没有 refreshToken")
            h = {
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": GLOBAL_BASE, "Referer": GLOBAL_BASE + "/",
                "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
                "X-Refresh-Token": self.refresh_token,
                "X-Auth-Refresh-Source": "workbuddy",
            }
            req = urllib.request.Request(
                GLOBAL_BASE + "/v2/plugin/auth/token/refresh", data=b"", headers=h, method="POST"
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            if body.get("code") != 0:
                raise RuntimeError(f"刷新失败: {body.get('msg')}")
            d = body["data"]
            self.access_token = d["accessToken"]
            if d.get("refreshToken"):
                self.refresh_token = d["refreshToken"]
            exp_in = d.get("expiresIn") or 0
            self.expires_at = time.time() + float(exp_in)
            log_err("令牌已刷新，新到期", time.strftime("%Y-%m-%d %H:%M", time.localtime(self.expires_at)))

    # ── 目录 / 额度 ──
    def catalog(self) -> list[dict]:
        req = urllib.request.Request(GLOBAL_BASE + "/v3/config", headers=self._common())
        with urllib.request.urlopen(req, timeout=45) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))

        def find(o, depth=0):
            if depth > 3:
                return None
            if isinstance(o, dict):
                for k in ("models", "modelList", "llmModels"):
                    if isinstance(o.get(k), list) and o[k]:
                        return o[k]
                for v in o.values():
                    got = find(v, depth + 1)
                    if got:
                        return got
            return None

        return find(doc) or []

    def credits(self) -> dict:
        body = json.dumps({"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca"}).encode()
        req = urllib.request.Request(
            GLOBAL_BASE + "/v2/billing/meter/get-user-resource",
            data=body, headers=self.chat_headers(), method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        accts = (((d.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        total_remain = sum(float(a.get("CapacityRemainPrecise") or a.get("CapacityRemain") or 0) for a in accts)
        total_size = sum(float(a.get("CapacitySizePrecise") or a.get("CapacitySize") or 0) for a in accts)
        return {
            "remaining": round(total_remain, 2),
            "total": round(total_size, 2),
            "packages": [
                {
                    "name": a.get("PackageName"), "remain": a.get("CapacityRemain"),
                    "size": a.get("CapacitySize"),
                    "cycleEnd": a.get("CycleEndTime"),
                }
                for a in accts
            ],
        }


# ─────────────────────────── 上游转发 ───────────────────────────

CHAT_PATH = "/v2/chat/completions"


def _prepare_payload(payload: dict) -> dict:
    """上游硬约束（实测）：

    - 首条消息必须是 ``system``（否则 400 code 11128）；
    - 完全不接受 ``developer`` 角色（任何位置都报同一个错）；
    - 必须 ``stream=true``（否则 400 code 11101）。

    有些客户端会把 system 改写成 ``developer``，所以这里统一
    折回 ``system``，再补一条兜底 system。
    """
    payload = dict(payload)
    msgs = []
    for m in payload.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "developer":
            m = {**m, "role": "system"}
        msgs.append(m)
    if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": "You are a helpful assistant."})
    payload["messages"] = msgs
    payload["stream"] = True
    return payload


def open_upstream(creds: Credentials, payload: dict):
    """返回 (response 对象, 最终 payload)。401 时刷新令牌重试一次。"""
    creds.ensure_fresh()
    data = json.dumps(_prepare_payload(payload)).encode("utf-8")
    try:
        req = urllib.request.Request(
            GLOBAL_BASE + CHAT_PATH, data=data, headers=creds.chat_headers(), method="POST"
        )
        return urllib.request.urlopen(req, timeout=600), payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code in (401, 403):
            log_err("上游 401/403，刷新令牌后重试")
            creds.refresh()
            req = urllib.request.Request(
                GLOBAL_BASE + CHAT_PATH, data=data, headers=creds.chat_headers(), method="POST"
            )
            return urllib.request.urlopen(req, timeout=600), payload
        raise UpstreamError(e.code, detail)


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def iter_sse(resp):
    """逐行产出上游 SSE 的 data 部分。"""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            return
        if chunk:
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                log("跳过无法解析的 SSE 帧:", chunk[:120])


def aggregate(events: list[dict], model: str) -> dict:
    """把 SSE 事件聚合成一个非流式 chat.completion。"""
    content, reasoning, tool_calls, finish = [], [], {}, "stop"
    cid, created, usage = "", int(time.time()), None
    for ev in events:
        cid = ev.get("id") or cid
        created = ev.get("created") or created
        if ev.get("usage"):
            usage = ev["usage"]
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    msg: dict = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return {
        "id": cid or f"wb-{int(time.time()*1000)}",
        "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage or {},
    }


# ─────────────────────────── HTTP 服务 ───────────────────────────

def _log_usage(model: str, usage: dict) -> None:
    """把每次请求的 token / 积分记到 wbbridge-usage.jsonl，便于核算成本。

    同时兼容 Chat 形状（prompt_tokens/completion_tokens）与 Responses 形状
    （input_tokens/output_tokens）。
    """
    path = os.environ.get("WBBRIDGE_USAGE_LOG")
    if not path or not usage:
        return
    try:
        def g(*keys, default=None):
            for k in keys:
                if usage.get(k) is not None:
                    return usage[k]
            return default

        details_in = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        details_out = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
        row = {
            "ts": int(time.time()), "model": model,
            "in": g("prompt_tokens", "input_tokens", default=0),
            "cached": g("prompt_cache_hit_tokens", "cached_tokens", default=0)
                      or details_in.get("cached_tokens") or 0,
            "out": g("completion_tokens", "output_tokens", default=0),
            "reasoning": details_out.get("reasoning_tokens") or 0,
            "credit": g("credit"),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


class _Server(ThreadingHTTPServer):
    """只做一件事：客户端断线不打堆栈。

    断线是常态——用户取消、Codex 在 429 重试风暴里放弃、桌面端切会话——
    默认 handle_error 会把整片 traceback 刷到 stderr，把真正的错误埋掉。
    """
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionError, OSError)):
            log(f"连接中断（{et.__name__}），忽略")
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    creds: Credentials = None  # type: ignore

    def log_message(self, fmt, *args):  # 静音默认访问日志
        log(fmt % args)

    # ── 工具 ──
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError) as e:      # 客户端已断开（如 429 重试途中取消）
            log(f"响应写入失败，客户端已断开（{type(e).__name__}）")

    def _err(self, status, message, code="upstream_error"):
        self._json({"error": {"message": message, "type": code, "code": status}}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ── 路由 ──
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/health", "/"):
                c = self.creds
                self._json({
                    "ok": True, "provider": "wbbridge-ai",
                    "uid": c.uid, "app_version": c.app_version,
                    "token_expires_at": c.expires_at,
                    "token_valid": not c.expired(),
                    "last_model": LAST_MODEL.get("model"),
                    "last_model_ts": LAST_MODEL.get("ts"),
                    "requests_seen": LAST_MODEL.get("count", 0),
                })
            elif path == "/wb/last-model":
                # 实际生效的模型只能由这里判定：代理是唯一看到真实 model 字段的一方。
                # Codex 会话内切换模型不会写回 config.toml，读配置必然读到旧的默认值。
                d = dict(LAST_MODEL)
                d["config_model"] = _config_model()
                d["config_synced"] = (d["config_model"] == LAST_MODEL.get("model"))
                d["sync_enabled"] = SYNC_CONFIG
                d["config_path"] = CODEX_CONFIG
                self._json(d)
            elif path == "/v1/models":
                models = self.creds.catalog()

                def cw_of(m):
                    return ((m.get("contextWindow") or {}).get("defaultLength")
                            or m.get("maxAllowedSize"))

                data = [
                    {"id": m.get("id"), "object": "model", "created": 0,
                     "owned_by": "workbuddy-ai",
                     "name": m.get("name"),
                     "credits": m.get("credits") or "",
                     "context_window": cw_of(m),
                     "max_output_tokens": m.get("maxOutputTokens")}
                    for m in models if m.get("id")
                ]
                # Codex 0.15x 的模型目录要求 {"models":[...]}，且条目字段名与 OpenAI
                # 形状完全不同（slug/display_name/base_instructions…）。缺了它会报
                # "Model metadata for X not found. Defaulting to fallback metadata"，
                # TUI 的模型名也会一直停在 loading。两个键都发，各取所需。
                codex_models = []
                for i, m in enumerate(models):
                    if not m.get("id"):
                        continue
                    codex_models.append({
                        "slug": m["id"],
                        "display_name": m.get("name") or m["id"],
                        "description": m.get("name") or m["id"],
                        "context_window": cw_of(m) or 200000,
                        "max_output_tokens": m.get("maxOutputTokens") or 24000,
                        "base_instructions": "default",
                        "shell_type": "shell_command",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": i,
                        "supports_reasoning_summaries": False,
                        "default_reasoning_summary": "none",
                        "support_verbosity": False,
                        "truncation_policy": {"mode": "bytes", "limit": 10000},
                        "experimental_supported_tools": [],
                        "supports_parallel_tool_calls": False,
                        "input_modalities": ["text"],
                        "default_reasoning_level": "low",
                        "supported_reasoning_levels": [
                            {"effort": e, "description": e}
                            for e in ("none", "low", "medium", "high")
                        ],
                    })
                self._json({"object": "list", "data": data, "models": codex_models})
            elif path == "/wb/credits":
                self._json(self.creds.credits())
            else:
                self._err(404, f"未知路径 {path}", "not_found")
        except UpstreamError as e:
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            self._err(e.status, e.detail)
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/responses":
            self._responses()
            return
        if path not in ("/v1/chat/completions", "/v1/completions"):
            self._err(404, f"未知路径 {path}", "not_found")
            return
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"请求体不是合法 JSON: {e}", "invalid_request")
            return

        model = payload.get("model") or "default-model"
        _note_model(model)
        want_stream = bool(payload.get("stream"))
        if os.environ.get("WBBRIDGE_DUMP"):
            try:
                d = dict(payload)
                m = d.get("messages") or []
                for msg in m:
                    c = msg.get("content")
                    if isinstance(c, str) and len(c) > 160:
                        msg["content"] = c[:160] + f"...<{len(c)} 字>"
                if d.get("tools"):
                    d["tools"] = [f"<{len(d['tools'])} 个工具>" ]
                with open(os.environ["WBBRIDGE_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d, ensure_ascii=False)[:4000] + "\n")
            except Exception:
                pass
        log("请求 model=%s stream=%s 顶层字段=%s" % (
            model, want_stream, sorted(k for k in payload if k != "messages")))
        log("消息数=%d 首条 role=%s" % (
            len(payload.get("messages") or []),
            (payload.get("messages") or [{}])[0].get("role")))

        try:
            resp, _ = open_upstream(self.creds, payload)
        except UpstreamError as e:
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            self._err(e.status, e.detail)
            return
        except Exception as e:
            self._err(502, f"{type(e).__name__}: {e}")
            return

        if want_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for ev in iter_sse(resp):
                    if ev.get("usage"):
                        _log_usage(model, ev["usage"])
                    out = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {out}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            # Windows 上客户端取消/断开会抛 ConnectionAbortedError(WinError 10053)，
            # 只捕 BrokenPipe/Reset 会漏掉它并打出整片堆栈。ConnectionError 是其父类，
            # 连同 OSError 一起兜住「对端已消失」的各种形态。
            except (ConnectionError, OSError) as e:
                log(f"客户端断开（{type(e).__name__}）")
            finally:
                resp.close()
            return

        # 非流式：本地聚合
        try:
            events = list(iter_sse(resp))
        finally:
            resp.close()
        agg = aggregate(events, model)
        _log_usage(model, agg.get("usage") or {})
        self._json(agg)

    def do_PUT(self):
        self.do_POST()

    # ── Responses API（Codex 0.153+ 唯一支持的 wire_api）──
    def _responses(self) -> None:
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"请求体不是合法 JSON: {e}", "invalid_request")
            return

        model = payload.get("model") or "default-model"
        _note_model(model)
        want_stream = payload.get("stream", True) is not False
        if os.environ.get("WBBRIDGE_DUMP"):
            try:
                d = dict(payload)
                d["input"] = f"<{len(payload.get('input') or [])} items>"
                if d.get("tools"):
                    d["tools"] = f"<{len(d['tools'])} 个工具>"
                with open(os.environ["WBBRIDGE_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write("[responses] " + json.dumps(d, ensure_ascii=False)[:3000] + "\n")
            except Exception:
                pass

        try:
            chat_payload = wbresponses.request_to_chat(payload)
        except Exception as e:
            self._err(400, f"请求转换失败: {type(e).__name__}: {e}", "invalid_request")
            return
        log("responses→chat model=%s 消息数=%d 工具=%d stream=%s" % (
            model, len(chat_payload["messages"]), len(chat_payload.get("tools") or []), want_stream))
        if os.environ.get("WBBRIDGE_DUMP"):
            try:
                roles = [(m.get("role"), len(str(m.get("content") or ""))) for m in chat_payload["messages"]]
                extra = sorted(k for k in chat_payload if k not in ("model", "messages", "stream"))
                with open(os.environ["WBBRIDGE_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write("[chat] keys=%s roles=%s\n" % (extra, roles[:12]))
            except Exception:
                pass
        if os.environ.get("WBBRIDGE_DUMP_CHAT"):
            try:
                with open(os.environ["WBBRIDGE_DUMP_CHAT"], "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(chat_payload, ensure_ascii=False))
            except Exception:
                pass

        try:
            resp, _ = open_upstream(self.creds, chat_payload)
        except UpstreamError as e:
            log_err(f"上游错误 {e.status}: {upstream_error_text(e.detail)}")
            self._err(e.status, e.detail)
            return
        except Exception as e:
            self._err(502, f"{type(e).__name__}: {e}")
            return

        if not want_stream:
            try:
                events = list(iter_sse(resp))
            finally:
                resp.close()
            final = wbresponses.aggregate_response(events, model)
            _log_usage(model, (events[-1].get("usage") if events else {}) or {})
            self._json(final)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for name, ev in wbresponses.stream_responses_events(iter_sse(resp), model):
                if name == "response.completed":
                    _log_usage(model, ev["response"].get("usage") or {})
                body = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"event: {name}\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (ConnectionError, OSError) as e:
            log(f"客户端断开（{type(e).__name__}）")
        finally:
            resp.close()


def main() -> int:
    global _VERBOSE
    wbdiscover.console_encodings()
    ap = argparse.ArgumentParser(description="WorkBuddy AI → OpenAI 兼容本地代理")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _VERBOSE = args.verbose

    # 预检：把「缺什么、怎么补」直接说清楚，别让新用户对着一条含糊报错猜。
    py = sys.executable
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        print(f"缺少依赖 cryptography（当前解释器：{py}）\n"
              f"请安装：\"{py}\" -m pip install cryptography", file=sys.stderr)
        return 3

    if not APP_DIR or not os.path.isfile(APP_DIR):
        print("找不到 WorkBuddy AI 桌面端主程序。\n"
              "请先安装并登录 WorkBuddy AI 桌面端（国际版），"
              "或用环境变量指定：WBBRIDGE_APP=<...>\\WorkBuddyAI.exe", file=sys.stderr)
        return 2

    if not AUTH_FILE or not os.path.exists(AUTH_FILE):
        print("找不到 WorkBuddy AI 凭据文件。\n"
              "请先启动 WorkBuddy AI 桌面端并登录（登录后凭据会自动写出），"
              "或用环境变量指定：WBBRIDGE_AUTH_FILE=<...>\\workbuddy-desktop-ai.info",
              file=sys.stderr)
        return 2

    print(f"桌面端：{APP_DIR}", file=sys.stderr)
    print(f"凭据：{AUTH_FILE}", file=sys.stderr)
    print("正在解密 WorkBuddy AI 凭据…", file=sys.stderr)
    try:
        Handler.creds = Credentials()
    except Exception as e:
        print(f"凭据解密失败：{type(e).__name__}: {e}\n"
              "常见原因：桌面端未登录、版本升级改了绑定名、或换了机器。",
              file=sys.stderr)
        return 2
    print(f"就绪 → http://{args.host}:{args.port}/v1   （模型列表 /v1/models，额度 /wb/credits）",
          file=sys.stderr)
    srv = _Server((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
