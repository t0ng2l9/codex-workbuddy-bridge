"""SessionStart hook：确保 WorkBuddy 本地代理在跑。

设计要点：
  - 无论成败都 exit 0 —— hook 失败不该让用户的会话起不来；
  - 代理已在跑就直接返回（探一次端口，很便宜）；
  - 拉起用分离进程 + 独立日志，不让它跟着会话退出。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PROXY = PLUGIN_ROOT / "bin" / "wbproxy.py"
sys.path.insert(0, str(PLUGIN_ROOT / "bin"))
import wbspawn  # noqa: E402  （无控制台启动）

DEFAULT_PORT = 8788


def port() -> int:
    try:
        return int(os.environ.get("WBBRIDGE_PROXY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def is_up(p: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.4):
            return True
    except OSError:
        return False


def _utf8_output() -> None:
    """输出目标是日志文件而非控制台，固定 UTF-8，避免受 PYTHONUTF8 / 代码页影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _utf8_output()
    p = port()
    if is_up(p):
        return 0
    if not PROXY.is_file():
        print(f"[workbuddy] 找不到代理脚本 {PROXY}", file=sys.stderr)
        return 0

    log = Path(tempfile.gettempdir()) / "wbbridge-proxy.log"
    try:
        # 用 wbspawn 取「不会带控制台窗口」的解释器：venv 里的 python.exe 是 uv 存根，
        # 转发时不传 creationflags，真实进程会自己弹一个可见控制台（实测踩过）。
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(PROXY), "--port", str(p)],
                               log=log, cwd=str(PLUGIN_ROOT))
    except OSError as e:
        print(f"[workbuddy] 拉起代理失败：{e}", file=sys.stderr)
        return 0

    # 等它就绪，最多 20 秒
    for _ in range(40):
        if is_up(p):
            print(f"[workbuddy] 代理已就绪 http://127.0.0.1:{p}/v1")
            return 0
        time.sleep(0.5)

    print(f"[workbuddy] 代理未在 20 秒内就绪，日志见 {log}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
