"""Build the Codex model catalog (wbbridge-catalog.json) from the local proxy.

The Codex desktop model picker is fed by app-server's `model/list`, which reads
the file that `model_catalog_json` in config.toml points at. Without that file
the picker falls back to Codex's built-in OpenAI catalog and no WorkBuddy model
is visible at all.

Usage:
    python gen-catalog.py                       # writes ~/.codex/wbbridge-catalog.json
    python gen-catalog.py --out D:/x/cat.json   # custom output
    python gen-catalog.py --port 8788           # custom proxy port
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
    """See wbdiscover.console_encodings: keep output encoding off PYTHONUTF8."""
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

# Fields Codex 0.16x expects on a Model that the OpenAI shape does not have.
# Without tool_mode / apply_patch_tool_type, newer builds decide there are no
# usable tools and send zero of them.
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

# Non-chat models (image/video); they do not belong in the picker
SKIP_PREFIXES = ("gpt-image", "seedance", "sora", "dall-e")

# Friendly display names for alias models (otherwise several aliases share a
# name and the picker looks like it has duplicates)
DISPLAY_NAMES = {
    "default-model": "Auto (smart routing)",
    "fast-model": "Fast",
    "balanced-model": "Balanced",
    "primary-model": "Primary",
    "deep-model": "Deep (extended thinking)",
    "deepseek-v4.1-flash-sg": "Deepseek-V4.1-Flash (SG)",
}


def fetch_models(port: int, timeout: int = 30) -> list[dict]:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Cannot reach the local proxy at {url}: {e}\n"
            "Start the proxy first (start-wbbridge.cmd) and make sure the WorkBuddy AI desktop app is signed in."
        )
    # The proxy returns both the OpenAI shape (data) and the Codex shape (models);
    # prefer the latter
    models = doc.get("models")
    if isinstance(models, list) and models:
        return models
    # Fallback: convert the OpenAI shape ourselves
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
    """Fill in fields, drop non-chat models, dedupe display names, renumber priority."""
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
            "priority": 0,                      # renumbered at the end
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

    # Dedupe display names (duplicates make the picker look wrong)
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
    ap = argparse.ArgumentParser(description="Generate the WorkBuddy model catalog for Codex")
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
        print(f"{len(raw)} models from the proxy -> {len(entries)} written "
              f"({len(raw) - len(entries)} non-chat/duplicate entries dropped)")
        print(f"Output: {args.out}")
        for e in entries:
            print(f'  {e["slug"]:<26} {e["display_name"]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
