#!/usr/bin/env python3
"""WorkBuddy AI -> OpenAI compatible local proxy.

Turns the WorkBuddy AI desktop app session into an OpenAI-compatible
`/v1/chat/completions` endpoint for any client. No external dependencies
(only the stdlib plus cryptography).

Upstream notes (measured, not guessed):
  - Credentials live in %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop-ai.info
    accessToken/refreshToken are AES-256-GCM envelopes; the key must be read by
    running WorkBuddy's own Electron binary (ELECTRON_RUN_AS_NODE=1) to reach the
    workbuddyStorage binding.
  - The chat endpoint https://www.workbuddy.ai/v2/chat/completions **requires stream=true**
    (otherwise 400 code 11101), and the **first message must be system** (otherwise 400 code 11128).
  - Model catalog: GET /v3/config; the UA must carry WorkBuddyAI/<version>.
  - Quota: POST /v2/billing/meter/get-user-resource.
  - usage.credit in the response is the number of credits actually spent.

Usage:
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

# ─────────────────────────── Config ──────────────────────────

GLOBAL_BASE = "https://www.workbuddy.ai"

# The desktop app path and the credential file are always auto-discovered
# (see wbdiscover); never hard-code a drive letter or a user name.
# The WBBRIDGE_APP / WBBRIDGE_AUTH_FILE environment variables override them.
APP_DIR = wbdiscover.find_app() or ""
AUTH_FILE = wbdiscover.find_auth_file() or ""
HELPER_JS = (
    'process.stdout.write(String(process._linkedBinding('
    '"electron_browser_workbuddy_storage").loggerGet()))'
)
DEFAULT_KEY = "wbbridge-local"          # clients may send anything; the local proxy does not check it
TOKEN_SKEW_SEC = 120                     # treat the token as expired 2 minutes early

_VERBOSE = False

# Effective-model tracking: the proxy is the only place that sees the real
# model field, so /wb/last-model answers "which model is actually in use".
LAST_MODEL: dict = {"model": None, "ts": 0, "count": 0}

# -- Model sync: switching models in the desktop picker only affects the current
# session and is never written back to config.toml, so "configured default" and
# "actually in use" drift apart (and anything reading the config reports the wrong
# model). Since the proxy sees the real model on every request, it writes the top
# level model key back so both sides agree.
CODEX_CONFIG = os.environ.get(
    "WBBRIDGE_CODEX_CONFIG",
    os.path.join(os.path.expanduser("~"), ".codex", "config.toml"),
)
SYNC_CONFIG = os.environ.get("WBBRIDGE_SYNC_CONFIG", "1") not in ("0", "false", "no")
_SYNC_LOCK = threading.Lock()
_CONFIG_MODEL: str | None = None          # model currently known to be in the config; avoids repeated I/O


def _config_model() -> str | None:
    """Read the first top-level `model = ...` in config.toml (stop at any [section])."""
    try:
        # newline="" preserves the original line endings; plain text mode folds
        # CRLF into LF, so writing back rewrites the whole file ("every line changed").
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
    """Set the top-level model to the one actually in use; touch only that line."""
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
            log(f"model sync failed (cannot read config): {e}")
            return
        changed = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("["):
                break                     # only touch top-level keys, never a [section]
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
            log("model sync: no top-level model key in the config, skipping")
            return
        try:
            tmp = CODEX_CONFIG + ".wbbridge-tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            os.replace(tmp, CODEX_CONFIG)
            _CONFIG_MODEL = model
            log(f"synced config default model -> {model}")
        except OSError as e:
            log(f"model sync failed (cannot write config): {e}")


def _note_model(model: str) -> None:
    LAST_MODEL["model"] = model
    LAST_MODEL["ts"] = int(time.time())
    LAST_MODEL["count"] = LAST_MODEL.get("count", 0) + 1
    _sync_config_model(model)


def log(*a):
    """Request tracing (silent by default; enable with --verbose)."""
    if _VERBOSE:
        print("[wbproxy]", *a, file=sys.stderr, flush=True)


def upstream_error_text(detail) -> str:
    """Flatten an upstream error body into one readable line.

    Truncating the raw JSON by character count cuts off the most useful hint
    (it sits behind a pile of bilingual fields), so parse code / msg /
    displayTips.zh and join them; fall back to truncation only if parsing fails.
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

    # Upstream sometimes nests another JSON document inside error.message
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
    """Error-level events, **never gated by --verbose**.

    Upstream errors (especially 429 / code 14003) are the only place a user can
    see why something failed; if they were only logged in verbose mode, anyone
    hitting a rate limit would find an empty log and have to ask.
    """
    print("[wbproxy]", *a, file=sys.stderr, flush=True)


# ─────────────────── Credentials: decrypt and refresh ───────────────────

def _aad(key_id: str, suite: int) -> bytes:
    """WorkBuddy AAD construction (mirrors its buildAuthenticatedContextAad)."""
    def lp(s: str) -> bytes:
        b = s.encode("utf-8")
        return struct.pack(">I", len(b)) + b

    return (b"WB-AAD\x00" + b"\x01" + lp("WBEV1") + lp("sym-v1")
            + struct.pack(">I", suite) + lp(key_id) + b"\x02" + b"\x00" + b"\x00")


def _electron_payload() -> dict:
    """Run WorkBuddy's Electron to obtain the at-rest key."""
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    r = subprocess.run(
        [APP_DIR, "-e", HELPER_JS], capture_output=True, text=True, timeout=60, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(
            f"WorkBuddy key helper failed (exit={r.returncode}); is the app installed? {APP_DIR}"
        )
    return json.loads(r.stdout.strip())


def _decrypt_field(wrapped: dict, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    inner = json.loads(base64.b64decode(wrapped["envelope"]))
    if inner.get("suite") != 1:
        raise RuntimeError(f"unknown envelope suite={inner.get('suite')}")
    pt = AESGCM(key).decrypt(
        base64.b64decode(inner["nonce"]),
        base64.b64decode(inner["ciphertext"]) + base64.b64decode(inner["authTag"]),
        _aad(inner["keyId"], inner["suite"]),
    )
    return pt.decode("utf-8")


class Credentials:
    """WorkBuddy AI credentials: read the desktop auth file -> decrypt -> refresh on expiry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: bytes | None = None
        # The gateway's channel check wants a conversation id; reuse one per proxy process.
        self.conversation_id = str(uuid.uuid4())
        self.access_token = ""
        self.refresh_token = ""
        self.uid = ""
        self.domain = ""
        self.expires_at = 0.0          # epoch seconds
        self.app_version = "5.5.2"
        self._load_app_version()
        self.reload()

    def _load_app_version(self) -> None:
        """The UA must carry the app version; the gateway routes /v3/config by it."""
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
                raise RuntimeError("unexpected key payload version")
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
            # expiresAt is in milliseconds
            raw_exp = auth.get("expiresAt")
            if isinstance(raw_exp, (int, float)) and raw_exp > 0:
                self.expires_at = raw_exp / 1000.0
            else:
                exp_in = auth.get("expiresIn") or 0
                self.expires_at = time.time() + float(exp_in)
            log(f"credentials loaded uid={self.uid[:8]}... expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.expires_at))}")

    # ── Request headers ──
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
        # Channel check (400 code 11128): the official client sends conversation and
        # request ids with every turn; without them the request is rejected as a
        # non-official client -- nothing to do with the system prompt.
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
        """POST /v2/plugin/auth/token/refresh (the only place X-Refresh-Token appears)."""
        with self._lock:
            if not self.refresh_token:
                raise RuntimeError("no refreshToken available")
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
                raise RuntimeError(f"refresh failed: {body.get('msg')}")
            d = body["data"]
            self.access_token = d["accessToken"]
            if d.get("refreshToken"):
                self.refresh_token = d["refreshToken"]
            exp_in = d.get("expiresIn") or 0
            self.expires_at = time.time() + float(exp_in)
            log_err("token refreshed; new expiry", time.strftime("%Y-%m-%d %H:%M", time.localtime(self.expires_at)))

    # ── Catalog / quota ──
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


# ────────────────────────── Upstream relay ──────────────────────────

CHAT_PATH = "/v2/chat/completions"


def _prepare_payload(payload: dict) -> dict:
    """Hard upstream constraints (measured):

    - the first message must be ``system`` (otherwise 400 code 11128);
    - the ``developer`` role is not accepted at all (same error anywhere);
    - ``stream=true`` is mandatory (otherwise 400 code 11101).

    Some clients rewrite system into ``developer``, so normalise everything back
    to ``system`` and prepend a fallback system message when there is none.
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
    """Return (response object, final payload). On 401, refresh the token and retry once."""
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
            log_err("upstream 401/403; refreshing the token and retrying")
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
    """Yield the data part of each upstream SSE frame."""
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
                log("skipping unparseable SSE frame:", chunk[:120])


def aggregate(events: list[dict], model: str) -> dict:
    """Aggregate SSE events into a single non-streaming chat.completion."""
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


# ──────────────────────────── HTTP server ───────────────────────────

def _log_usage(model: str, usage: dict) -> None:
    """Append per-request token / credit usage to wbbridge-usage.jsonl for cost tracking.

    Handles both the Chat shape (prompt_tokens/completion_tokens) and the
    Responses shape (input_tokens/output_tokens).
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
    """Do one thing: never dump a traceback when a client disconnects.

    Disconnects are normal -- the user cancels, Codex gives up during a 429 retry
    storm, the desktop app switches conversations -- and the default
    handle_error floods stderr with a full traceback, burying the real error.
    """
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et is not None and issubclass(et, (ConnectionError, OSError)):
            log(f"connection dropped ({et.__name__}), ignoring")
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    creds: Credentials = None  # type: ignore

    def log_message(self, fmt, *args):  # silence the default access log
        log(fmt % args)

    # ── Helpers ──
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError) as e:      # client already gone (e.g. cancelled mid 429 retry)
            log(f"write failed, client disconnected ({type(e).__name__})")

    def _err(self, status, message, code="upstream_error"):
        self._json({"error": {"message": message, "type": code, "code": status}}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    # ── Routing ──
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
                # Only this endpoint can report the effective model: the proxy is the one place
                # that sees the real model field. Switching models inside a Codex session
                # never writes back to config.toml, so reading the config gives a stale default.
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
                # Codex 0.15x expects the catalog as {"models":[...]} with entry fields that are
                # completely different from the OpenAI shape (slug/display_name/
                # base_instructions...). Without them it logs "Model metadata for X not
                # found. Defaulting to fallback metadata" and the TUI model name stays on
                # "loading" forever. Send both keys; each consumer picks what it needs.
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
                self._err(404, f"unknown path {path}", "not_found")
        except UpstreamError as e:
            log_err(f"upstream error {e.status}: {upstream_error_text(e.detail)}")
            self._err(e.status, e.detail)
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/responses":
            self._responses()
            return
        if path not in ("/v1/chat/completions", "/v1/completions"):
            self._err(404, f"unknown path {path}", "not_found")
            return
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"request body is not valid JSON: {e}", "invalid_request")
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
                        msg["content"] = c[:160] + f"...<{len(c)} chars>"
                if d.get("tools"):
                    d["tools"] = [f"<{len(d['tools'])} tools>"]
                with open(os.environ["WBBRIDGE_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d, ensure_ascii=False)[:4000] + "\n")
            except Exception:
                pass
        log("request model=%s stream=%s top-level keys=%s" % (
            model, want_stream, sorted(k for k in payload if k != "messages")))
        log("messages=%d first role=%s" % (
            len(payload.get("messages") or []),
            (payload.get("messages") or [{}])[0].get("role")))

        try:
            resp, _ = open_upstream(self.creds, payload)
        except UpstreamError as e:
            log_err(f"upstream error {e.status}: {upstream_error_text(e.detail)}")
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
            # On Windows a client cancel/disconnect raises ConnectionAbortedError
            # (WinError 10053); catching only BrokenPipe/Reset misses it and dumps a
            # traceback. ConnectionError is its parent, so catching it plus OSError
            # covers every "peer is gone" shape.
            except (ConnectionError, OSError) as e:
                log(f"client disconnected ({type(e).__name__})")
            finally:
                resp.close()
            return

        # Non-streaming: aggregate locally
        try:
            events = list(iter_sse(resp))
        finally:
            resp.close()
        agg = aggregate(events, model)
        _log_usage(model, agg.get("usage") or {})
        self._json(agg)

    def do_PUT(self):
        self.do_POST()

    # ── Responses API (the only wire_api Codex 0.153+ supports) ──
    def _responses(self) -> None:
        try:
            payload = self._body()
        except Exception as e:
            self._err(400, f"request body is not valid JSON: {e}", "invalid_request")
            return

        model = payload.get("model") or "default-model"
        _note_model(model)
        want_stream = payload.get("stream", True) is not False
        if os.environ.get("WBBRIDGE_DUMP"):
            try:
                d = dict(payload)
                d["input"] = f"<{len(payload.get('input') or [])} items>"
                if d.get("tools"):
                    d["tools"] = f"<{len(d['tools'])} tools>"
                with open(os.environ["WBBRIDGE_DUMP"], "a", encoding="utf-8") as fh:
                    fh.write("[responses] " + json.dumps(d, ensure_ascii=False)[:3000] + "\n")
            except Exception:
                pass

        try:
            chat_payload = wbresponses.request_to_chat(payload)
        except Exception as e:
            self._err(400, f"request conversion failed: {type(e).__name__}: {e}", "invalid_request")
            return
        log("responses->chat model=%s messages=%d tools=%d stream=%s" % (
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
            log_err(f"upstream error {e.status}: {upstream_error_text(e.detail)}")
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
            log(f"client disconnected ({type(e).__name__})")
        finally:
            resp.close()


def main() -> int:
    global _VERBOSE
    wbdiscover.console_encodings()
    ap = argparse.ArgumentParser(description="WorkBuddy AI -> OpenAI compatible local proxy")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    _VERBOSE = args.verbose

    # Preflight: spell out what is missing and how to fix it instead of leaving
    # a new user to guess at a vague error.
    py = sys.executable
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except ImportError:
        print(f"missing dependency: cryptography (interpreter: {py})\n"
              f"install it with: \"{py}\" -m pip install cryptography", file=sys.stderr)
        return 3

    if not APP_DIR or not os.path.isfile(APP_DIR):
        print("Could not find the WorkBuddy AI desktop executable.\n"
              "Install and sign in to the WorkBuddy AI desktop app (international build),"
              "or point to it with WBBRIDGE_APP=<...>\\WorkBuddyAI.exe", file=sys.stderr)
        return 2

    if not AUTH_FILE or not os.path.exists(AUTH_FILE):
        print("Could not find the WorkBuddy AI credential file.\n"
              "Start the WorkBuddy AI desktop app and sign in (the file is written on login),"
              "or point to it with WBBRIDGE_AUTH_FILE=<...>\\workbuddy-desktop-ai.info",
              file=sys.stderr)
        return 2

    print(f"desktop app: {APP_DIR}", file=sys.stderr)
    print(f"credentials: {AUTH_FILE}", file=sys.stderr)
    print("decrypting WorkBuddy AI credentials...", file=sys.stderr)
    try:
        Handler.creds = Credentials()
    except Exception as e:
        print(f"failed to decrypt credentials: {type(e).__name__}: {e}\n"
              "Common causes: not signed in, a version update renamed the binding,"
              "or the credentials came from another machine.",
              file=sys.stderr)
        return 2
    print(f"ready -> http://{args.host}:{args.port}/v1   (models: /v1/models, quota: /wb/credits)",
          file=sys.stderr)
    srv = _Server((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
