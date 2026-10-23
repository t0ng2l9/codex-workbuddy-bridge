"""给插件清单打一个 cachebuster 版本号。

Codex 把插件按版本号物化到 `plugins/cache/<市场>/<插件>/<版本>/`，**版本号不变就不会重新读取**。
所以本地改完插件要重装时，先跑这个脚本把 `version` 换成一个单调递增的 semver：

    1.2.2  ->  1.2.2-20261013120000

用的是 semver 的 prerelease 段，所以对解析仍然合法；时间戳取 UTC，保证单调递增。

用法：
    python bump-plugin.py <插件目录>
    python bump-plugin.py <插件目录> --set 2.0.0     # 手动指定（发布时用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 与官方 validator 一致的严格 semver
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# 已经带过时间戳后缀，替换掉而不是叠加
CACHEBUSTER_RE = re.compile(r"-\d{14}$")


def strip_cachebuster(version: str) -> str:
    return CACHEBUSTER_RE.sub("", version)


def console_encodings() -> None:
    """按控制台代码页重配输出，避免中文乱码（见 wbdiscover 的说明）。"""
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


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="给 Codex 插件打 cachebuster 版本号")
    ap.add_argument("plugin_path", help="插件根目录（含 .codex-plugin/plugin.json）")
    ap.add_argument("--set", metavar="VERSION",
                    help="显式指定版本（发布时用），而不是追加时间戳")
    a = ap.parse_args()

    manifest_path = Path(a.plugin_path).expanduser() / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file():
        print(f"找不到插件清单：{manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"读不了插件清单：{e}", file=sys.stderr)
        return 1

    old = str(manifest.get("version") or "")
    if SEMVER_RE.fullmatch(old) is None:
        print(f"现有 version 不是严格 semver，拒绝改：{old!r}", file=sys.stderr)
        return 1

    if a.set:
        new = a.set
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        new = f"{strip_cachebuster(old)}-{stamp}"

    if SEMVER_RE.fullmatch(new) is None:
        print(f"新版本号不是严格 semver：{new!r}", file=sys.stderr)
        return 1

    manifest["version"] = new
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    name = manifest.get("name", "?")
    print(f"{name}: {old} -> {new}")
    print("接着重装：codex plugin add " + f"{name}@<市场名>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
