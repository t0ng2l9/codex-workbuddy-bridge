"""按插件的启用状态看护本地代理：插件激活则代理在跑，未激活则停掉代理。

为什么需要它：Codex 的 hook 只在**会话开始**时触发一次，管不了三种情况 ——
会话进行中代理挂掉、机器重启后还没开会话、压根不用 Codex 时。这个看护进程补上，
判据直接取自 Codex 的插件状态，所以“激活/停用插件”就等于“开关代理”。

判据（两者同时成立才算激活）：
  1. config.toml 里 `[plugins."<name>@<market>"]` 的 `enabled` 为 true（或该插件段存在且未显式关掉）
  2. 插件已安装（cache 目录下有产物）

代理只认「本包起的那个」：停之前会核对进程命令行里是否含本包的 wbproxy.py，
不会误杀端口上的其它程序。

用法：
    python watch_plugin.py                    常驻看护（默认 5 秒一轮）
    python watch_plugin.py --once             只对账一次（测试用）
    python watch_plugin.py --status           打印当前判定，不改动任何东西
    python watch_plugin.py --install-autostart    装登录自启（计划任务）
    python watch_plugin.py --remove-autostart     移除登录自启
"""
from __future__ import annotations

import argparse
import os

import wbspawn
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN = PLUGIN_ROOT / "bin"
PROXY = BIN / "wbproxy.py"

PLUGIN_NAME = "workbuddy-bridge"
MARKETPLACE = "wbbridge"
DEFAULT_PORT = 8788
TASK_NAME = "workbuddy-bridge-watch"

LOG = Path(tempfile.gettempdir()) / "wbbridge-watch.log"
LOCK = Path(tempfile.gettempdir()) / "wbbridge-watch.lock"


def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log_err(*a) -> None:
    """状态变化与错误，**永远记录**（照 wbproxy 的分法：流水才该静音）。"""
    line = " ".join(str(x) for x in a)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def port() -> int:
    try:
        return int(os.environ.get("WBBRIDGE_PROXY_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def config_path() -> Path:
    return Path(os.environ.get("WBBRIDGE_CODEX_CONFIG") or (codex_home() / "config.toml"))


# ── 判据 1：config.toml 里的插件启用位 ────────────────────────
def plugin_enabled(cfg: Path | None = None) -> tuple[bool, str]:
    """返回 (是否激活, 说明)。缺 config 或没有该插件段都算未激活。"""
    cfg = cfg or config_path()
    if not cfg.is_file():
        return False, f"找不到配置 {cfg}"
    try:
        import tomllib
        with open(cfg, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        return False, f"解析配置失败：{type(e).__name__}: {e}"

    plugins = data.get("plugins") or {}
    key = f"{PLUGIN_NAME}@{MARKETPLACE}"
    if key not in plugins:
        return False, f"配置里没有 [{key}]（插件未安装或被移除）"
    entry = plugins.get(key) or {}
    enabled = entry.get("enabled", True)      # 段在但没写 enabled → 视为启用（与 Codex 默认一致）
    if enabled:
        return True, f"[{key}] enabled=true"
    return False, f"[{key}] enabled=false（已在 Codex 里停用）"


# ── 判据 2：插件确实装在盘上 ─────────────────────────────────
def cache_root() -> Path:
    return codex_home() / "plugins" / "cache" / MARKETPLACE / PLUGIN_NAME


def plugin_installed() -> tuple[bool, str]:
    root = cache_root()
    if not root.is_dir():
        return False, f"插件缓存不存在：{root}"
    versions = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
    if not versions:
        return False, f"插件缓存为空：{root}"
    return True, f"已安装版本 {versions[-1].name}"


def is_active(cfg: Path | None = None) -> tuple[bool, str]:
    ok1, why1 = plugin_enabled(cfg)
    if not ok1:
        return False, why1
    ok2, why2 = plugin_installed()
    if not ok2:
        return False, why2
    return True, f"激活（{why1}；{why2}）"


# ── 代理进程：识别、启动、停止 ───────────────────────────────
def _netstat_pids(p: int) -> list[str]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.decode("cp936", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{p}"):
                pids.append(parts[4])
    return pids


def _exe_args(pid: str) -> str:
    ps = (f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def proxy_running(p: int | None = None) -> tuple[bool, str]:
    p = p or port()
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.5):
            return True, "端口在监听"
    except OSError:
        return False, "端口未监听"


def our_pids(p: int | None = None) -> list[str]:
    """只听我们 8788 上、且命令行含本包 wbproxy 的进程 —— 不碰别人的程序。"""
    p = p or port()
    out = []
    for pid in _netstat_pids(p):
        if "wbproxy" in _exe_args(pid):
            out.append(pid)
    return out


def start_proxy(p: int | None = None) -> bool:
    p = p or port()
    if not PROXY.is_file():
        log_err(f"启动失败：找不到 {PROXY}")
        return False
    logf = Path(tempfile.gettempdir()) / "wbbridge-proxy.log"
    try:
        # 见 wbspawn：venv 的 python.exe 是 uv 存根，转发不传 flags → 真实进程弹控制台。
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(PROXY), "--port", str(p)],
                               log=logf, cwd=str(PLUGIN_ROOT))
    except OSError as e:
        log_err(f"启动代理失败：{e}")
        return False
    for _ in range(40):
        time.sleep(0.5)
        if proxy_running(p)[0]:
            log_err(f"代理已启动 → http://127.0.0.1:{p}/v1")
            return True
    log_err(f"代理启动后 20 秒仍未就绪（日志 {logf}）")
    return False


def stop_proxy(p: int | None = None) -> bool:
    p = p or port()
    pids = our_pids(p)
    if not pids:
        return False
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log_err(f"结束进程 {pid} 失败：{e}")
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.4)
        if not proxy_running(p)[0]:
            log_err(f"插件未激活 → 已停止代理（PID {', '.join(pids)}）")
            return True
    log_err(f"停止代理超时（PID {', '.join(pids)}）")
    return False


# ── 单实例 ───────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    try:
        subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, timeout=30)
        return str(pid) in r.stdout.decode("cp936", "replace")
    except (OSError, subprocess.SubprocessError):
        return False


def acquire_lock() -> bool:
    """抢单实例锁。

    用 O_CREAT|O_EXCL 原子创建 —— 上一版是「先看存在、再写」，两个进程同时启动时
    都能通过检查，于是并存了两个看护（实测踩到）。锁里记 pid，只在原持有者已死时才清理。
    """
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return True
        except FileExistsError:
            # 锁在：看持有者是否还活着；死了就当陈锁清掉再试一次
            try:
                old_pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                old_pid = 0
            if old_pid and _pid_alive(old_pid) and old_pid != os.getpid():
                return False
            try:
                LOCK.unlink(missing_ok=True)
            except OSError:
                return True          # 清不掉就别因此罢工
        except OSError:
            return True              # 锁文件写不了也不该阻断看护
    return False


# ── 对账 ─────────────────────────────────────────────────────
def reconcile(cfg: Path | None = None, p: int | None = None, quiet: bool = False) -> str:
    """让代理状态跟上插件状态。返回本次动作描述。"""
    p = p or port()
    active, why = is_active(cfg)
    running, _ = proxy_running(p)

    if active and not running:
        if not quiet:
            log_err(f"插件激活但代理没跑（{why}）→ 启动代理")
        start_proxy(p)
        return "started"
    if not active and running:
        pids = our_pids(p)
        if not pids:
            # 端口上有程序在监听，但不是本包的代理：不是我们该管的，绝不误杀。
            return "foreign-port" if not quiet else "ok"
        if not quiet:
            log_err(f"插件未激活（{why}）→ 停止代理")
        stop_proxy(p)
        return "stopped"
    return "ok" if active else "idle"


def show_status(cfg: Path | None = None, p: int | None = None) -> int:
    p = p or port()
    active, why = is_active(cfg)
    running, rwhy = proxy_running(p)
    pids = our_pids(p) if running else []
    print(f"  插件激活 : {active}   （{why}）")
    print(f"  代理运行 : {running} （{rwhy}）")
    if pids:
        print(f"  本包代理 PID : {', '.join(pids)}")
    print(f"  期望状态 : {'代理应在跑' if active else '代理应停止'}"
          f"  →  {'一致' if active == running else '不一致，需对账'}")
    return 0


# ── 登录自启 ─────────────────────────────────────────────────
# 用「启动文件夹」而不是计划任务：schtasks /Create 通常需要管理员权限
# （实测 "错误: 拒绝访问。"），而启动文件夹是按用户的、免权限、登录即跑。
# 用 .vbs 包一层是为了隐藏控制台窗口（pythonw 已无窗口，双保险）。

AUTOSTART_VBS = "workbuddy-bridge-watch.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_file() -> Path:
    return startup_dir() / AUTOSTART_VBS


def watcher_exe() -> str:
    """优先 pythonw.exe（无控制台窗口）。"""
    return wbspawn.no_console_python()


def _vbs(path_exe: str, path_script: str) -> str:
    """生成自启 VBS。

    用 Chr(34) 显式拼引号，而不是在字面量里数 `"` —— 直接写字符串字面量时
    引号数量算错就会得到「未结束的字符串常量」编译错误（踩过）。
    """
    return (
        "' 由 workbuddy-bridge 安装：登录时启动代理看护（隐藏窗口）。\r\n"
        "' 删除本文件即取消自启。\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'exe = "{path_exe}"\r\n'
        f'script = "{path_script}"\r\n'
        'q = Chr(34)\r\n'
        "cmd = q & exe & q & \" \" & q & script & q\r\n"
        "sh.Run cmd, 0, False\r\n"
    )


def install_autostart() -> int:
    d = startup_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log_err(f"无法访问启动文件夹 {d}：{e}")
        print(f"  !! 无法访问启动文件夹：{e}")
        return 1

    target = autostart_file()
    exe, script = watcher_exe(), str(Path(__file__).resolve())
    try:
        # UTF-16LE + BOM：WSH 认这个，且用户名/路径含非 ASCII 时也安全
        target.write_text(_vbs(exe, script), encoding="utf-16")
    except OSError as e:
        log_err(f"写自启文件失败 {target}：{e}")
        print(f"  !! 写自启文件失败：{e}")
        return 1
    print(f"  已安装登录自启：{target}")
    print(f"    {exe} {script}")

    # 立刻起一个，省得等到下次登录
    if acquire_lock():
        try:
            wbspawn.spawn_detached([wbspawn.no_console_python(), script])
            print("  看护已开始运行")
        except OSError as e:
            print(f"  !! 立刻启动看护失败（登录后仍会自启）：{e}")
    else:
        print("  看护已在运行中")
    print(f"  看护日志：{LOG}")
    return 0


def remove_autostart() -> int:
    target = autostart_file()
    if not target.is_file():
        print("  没有安装自启（文件不存在）")
        return 0
    try:
        target.unlink()
    except OSError as e:
        print(f"  !! 删除自启文件失败：{e}")
        return 1
    print(f"  已移除登录自启：{target}")
    print("  注意：正在跑的看护进程不会被自动结束，可手动 taskkill。")
    return 0


def autostart_installed() -> bool:
    try:
        return autostart_file().is_file()
    except OSError:
        return False


def main() -> int:
    _utf8()
    ap = argparse.ArgumentParser(description="按 Codex 插件启用状态看护本地代理")
    ap.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None, help="指定 config.toml（默认 $CODEX_HOME/config.toml）")
    ap.add_argument("--once", action="store_true", help="只对账一次")
    ap.add_argument("--status", action="store_true", help="打印判定，不改动")
    ap.add_argument("--install-autostart", action="store_true", help="装登录自启")
    ap.add_argument("--remove-autostart", action="store_true", help="移除登录自启")
    a = ap.parse_args()

    cfg = Path(a.config) if a.config else None
    p = a.port or port()

    if a.status:
        return show_status(cfg, p)
    if a.install_autostart:
        return install_autostart()
    if a.remove_autostart:
        return remove_autostart()
    if a.once:
        print("  " + reconcile(cfg, p))
        return 0

    if not acquire_lock():
        print(f"  已有看护进程在跑（{LOCK}）；如需重启用 --once 或先删该文件。")
        return 1

    log_err(f"看护启动 pid={os.getpid()} 间隔={a.interval}s 配置={cfg or config_path()}")
    try:
        while True:
            try:
                reconcile(cfg, p)
            except Exception as e:      # 单轮出错不能让看护整体退出
                log_err(f"对账异常（已跳过本轮）：{type(e).__name__}: {e}")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log_err("看护退出（KeyboardInterrupt）")
    finally:
        LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
