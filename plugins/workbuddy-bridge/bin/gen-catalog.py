"""从本地代理生成 Codex 用的模型目录（wbbridge-catalog.json）。

Codex 桌面端的「模型切换」列表来自 app-server 的 `model/list`，而它读取 config.toml 里
`model_catalog_json` 指向的文件。不设这个文件，列表就回落到 Codex 内置的 OpenAI 目录，
看不到任何 WorkBuddy 模型。

用法：
    python gen-catalog.py                       # 写 ~/.codex/wbbridge-catalog.json
    python gen-catalog.py --out D:/x/cat.json   # 自定义输出
    python gen-catalog.py --port 8788           # 自定义代理端口
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request


def console_encodings() -> None:
    """见 wbdiscover.console_encodings：让中文输出不依赖 PYTHONUTF8。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    enc = f"cp{cp}" if cp else "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass

# Codex 0.16x 的 Model 结构里有、而 OpenAI 形状没有的字段。
# 缺 tool_mode / apply_patch_tool_type 时新版本会认为「没有可用工具」而发 0 个工具。
EXTRA_FIELDS: dict = {
    "tool_mode": "parameters",
    "apply_patch_tool_type": "freeform",
    "supports_search_tool": False,
    "supports_experimental_context": False,
    "use_responses_lite": False,
    "supports_reasoning_effort_updates": False,
    "node_repl_disabled": True,
    "include_permissions_instructions": False,
    "include_environment_context": False,
}

# 非对话模型（图片/视频），不属于模型切换列表
SKIP_PREFIXES = ("gpt-image", "seedance", "sora", "dall-e")

# 别名模型的友好显示名（否则多个别名会同名，列表里看着像重复项）
DISPLAY_NAMES = {
    "default-model": "Auto (智能选路)",
    "fast-model": "Fast (快速)",
    "balanced-model": "Balanced (均衡)",
    "primary-model": "Primary (主力)",
    "deep-model": "Deep (深度思考)",
    "deepseek-v4.1-flash-sg": "Deepseek-V4.1-Flash (SG)",
}


def fetch_models(port: int, timeout: int = 30) -> list[dict]:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上本地代理 {url}：{e}\n"
            "请先启动代理（start-wbbridge.cmd），并确认 WorkBuddy AI 桌面端已登录。"
        )
    # 代理同时返回 OpenAI 形状(data) 与 Codex 形状(models)；优先用后者
    models = doc.get("models")
    if isinstance(models, list) and models:
        return models
    # 回退：从 OpenAI 形状自行转换
    out = []
    for m in doc.get("data") or []:
        if not m.get("id"):
            continue
        out.append({
            "slug": m["id"],
            "display_name": m.get("name") or m["id"],
            "description": m.get("name") or m["id"],
            "context_window": m.get("context_window") or 200_000,
            "max_output_tokens": m.get("max_output_tokens") or 24_000,
        })
    return out


def build_catalog(models: list[dict]) -> list[dict]:
    """补字段、剔非对话模型、去重显示名、重排 priority。"""
    entries: list[dict] = []
    seen_slug: set[str] = set()
    for m in models:
        slug = m.get("slug") or m.get("id")
        if not slug or slug in seen_slug:
            continue
        if slug.startswith(SKIP_PREFIXES):
            continue
        seen_slug.add(slug)

        e = {
            "slug": slug,
            "display_name": DISPLAY_NAMES.get(slug) or m.get("display_name") or slug,
            "description": DISPLAY_NAMES.get(slug) or m.get("description") or slug,
            "context_window": m.get("context_window") or 200_000,
            "max_output_tokens": m.get("max_output_tokens") or 24_000,
            "base_instructions": "default",
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 0,                      # 稍后统一重排
            "supports_reasoning_summaries": False,
            "default_reasoning_summary": "none",
            "support_verbosity": False,
            "truncation_policy": {"mode": "bytes", "limit": 10_000},
            "experimental_supported_tools": [],
            "supports_parallel_tool_calls": False,
            "input_modalities": ["text"],
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": x, "description": x}
                for x in ("none", "low", "medium", "high")
            ],
        }
        e.update(EXTRA_FIELDS)
        entries.append(e)

    # 显示名去重（同名会让切换列表看起来有重复项）
    names: dict[str, int] = {}
    for e in entries:
        names[e["display_name"]] = names.get(e["display_name"], 0) + 1
    for e in entries:
        if names[e["display_name"]] > 1:
            e["display_name"] = f'{e["display_name"]} [{e["slug"]}]'

    for i, e in enumerate(entries):
        e["priority"] = i
    return entries


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="生成 Codex 用的 WorkBuddy 模型目录")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WBBRIDGE_PROXY_PORT", 8788)))
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".codex", "wbbridge-catalog.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    raw = fetch_models(args.port)
    entries = build_catalog(raw)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"models": entries}, fh, ensure_ascii=False, indent=1)

    if not args.quiet:
        print(f"代理模型 {len(raw)} 个 → 写入 {len(entries)} 个（已剔除 {len(raw) - len(entries)} 个非对话/重复项）")
        print(f"输出：{args.out}")
        for e in entries:
            print(f'  {e["slug"]:<26} {e["display_name"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
