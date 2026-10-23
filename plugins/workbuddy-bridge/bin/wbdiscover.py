"""路径与运行时自动发现。

让这个包能在**任意机器**上跑起来：WorkBuddy 桌面端装在哪、凭据文件叫什么、
Python 和 Codex 在不在 PATH 里，一律自动找，不写死。

所有发现函数都遵循同一优先级：**环境变量覆盖 > 自动探测**。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── 环境变量名（同时被 wbproxy / setup / 启动脚本读取）──
ENV_APP = "WBBRIDGE_APP"
ENV_AUTH = "WBBRIDGE_AUTH_FILE"
ENV_PYTHON = "WBBRIDGE_PYTHON"
ENV_CODEX = "WBBRIDGE_CODEX"

APP_EXE_NAMES = ("WorkBuddyAI.exe", "CodeBuddy.exe", "WorkBuddy.exe")
APP_DIR_NAMES = ("WorkBuddyAI", "WorkBuddy AI", "CodeBuddy", "CodeBuddyAI")

# 凭据文件名：国际版优先，国内版兜底（两者可能同时存在，且属于不同账号）
AUTH_NAMES = ("workbuddy-desktop-ai.info", "workbuddy-desktop.info")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def console_encodings() -> None:
    """让 stdout/stderr 的编码匹配控制台代码页，避免中文乱码。

    有些环境（例如某些 IDE / Agent 宿主）会设 PYTHONUTF8=1，Python 便输出 UTF-8，而 Windows 控制台
    默认是 cp936，中文就变成乱码。这里统一按**控制台输出代码页**重配，于是：

      - 终端里显示正常；
      - 重定向到文件时也和 .cmd 自身的中文（cmd 用 OEM 代码页输出）保持一致，
        不会出现半截 cp936、半截 UTF-8 的混合文件。

    不依赖 chcp —— 改 chcp 会打断正在执行的 .cmd 里 cp936 编码的中文。
    """
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


# ── WorkBuddy 桌面端可执行文件 ──────────────────────────────
def _app_from_registry() -> str | None:
    """NSIS 安装会写卸载项，UninstallString 里的目录就是安装目录。"""
    if sys.platform != "win32":
        return None
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p=@('HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
        "Get-ItemProperty $p | "
        "Where-Object { $_.DisplayName -match 'WorkBuddy|CodeBuddy' } | "
        "Select-Object -First 4 -ExpandProperty UninstallString"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        # 形如  "D:\...\WorkBuddyAI\Uninstall WorkBuddyAI.exe" /currentuser
        path = line.split('"')[0] if line.startswith('"') else line
        path = path.split(" /")[0].strip().strip('"')
        d = Path(path).parent
        for exe in APP_EXE_NAMES:
            cand = d / exe
            if cand.is_file():
                return str(cand)
    return None


def _app_from_process() -> str | None:
    """桌面端正在跑的话，直接问它。"""
    if sys.platform != "win32":
        return None
    names = "' or name='".join(n.replace(".exe", ".exe") for n in APP_EXE_NAMES)
    try:
        r = subprocess.run(
            ["wmic", "process", "where", f"name='{names}'",
             "get", "ExecutablePath", "/format:csv"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if parts and parts[-1].lower().endswith(".exe") and Path(parts[-1]).is_file():
            return parts[-1]
    return None


def _app_from_common_paths() -> str | None:
    roots = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("APPDATA"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    seen: list[Path] = []
    for root in roots:
        if not root:
            continue
        for d in APP_DIR_NAMES:
            for sub in ("Programs", ""):
                base = Path(root) / sub / d if sub else Path(root) / d
                if base not in seen:
                    seen.append(base)
    # 非系统盘上的同名目录（很多人装到 D:）
    for drive in "CDEFGH":
        for d in APP_DIR_NAMES:
            seen.append(Path(f"{drive}:/Programs/{d}"))
            seen.append(Path(f"{drive}:/Program Files/{d}"))
    for base in seen:
        for exe in APP_EXE_NAMES:
            cand = base / exe
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
    return None


def find_app() -> str | None:
    """返回 WorkBuddy 桌面端主程序路径；找不到返回 None。

    显式设置了 WBBRIDGE_APP 就原样采信（哪怕路径不存在）——这样写错路径时
    体检会如实报「找不到」，而不是悄悄回退到自动发现、让人以为配置生效了。
    """
    override = _env(ENV_APP)
    if override:
        return override
    for probe in (_app_from_registry, _app_from_process, _app_from_common_paths):
        got = probe()
        if got:
            return got
    return None


# ── 凭据文件 ────────────────────────────────────────────────
def _auth_dirs() -> list[Path]:
    la = os.environ.get("LOCALAPPDATA")
    out: list[Path] = []
    if la:
        out.append(Path(la) / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    # 部分版本把数据放在用户目录下
    out.append(Path.home() / ".codebuddy" / "auth")
    return out


def find_auth_files() -> list[str]:
    """列出所有可用的凭据文件（国际版在前）。"""
    found: list[str] = []
    for d in _auth_dirs():
        for name in AUTH_NAMES:
            p = d / name
            try:
                if p.is_file() and p.stat().st_size > 0:
                    found.append(str(p))
            except OSError:
                continue
    return found


def find_auth_file() -> str | None:
    override = _env(ENV_AUTH)
    if override:
        return override
    files = find_auth_files()
    return files[0] if files else None


# ── Python 解释器（供 .cmd 启动脚本用）────────────────────
def _python_candidates() -> list[str]:
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    home = Path.home()
    out = [
        os.path.join(la, "Programs", "Python", "Python313", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python311", "python.exe"),
        os.path.join(la, "Programs", "Python", "Python310", "python.exe"),
        os.path.join(pf, "Python313", "python.exe"),
        os.path.join(pf, "Python312", "python.exe"),
        os.path.join(pf, "Python311", "python.exe"),
        os.path.join(pf, "Python310", "python.exe"),
    ]
    # Codex 桌面端自带一份 Python（3.12 + cryptography），没装系统 Python 的机器靠它也能跑。
    # 路径形如 ~/.cache/codex-runtimes/<runtime>/dependencies/python/python.exe
    for base, pat in ((home / ".cache" / "codex-runtimes", "*/dependencies/python/python.exe"),
                      (home / ".codex" / "runtimes", "*/python/python.exe")):
        try:
            out.extend(str(p) for p in sorted(base.glob(pat)))
        except OSError:
            pass
    return out


def find_python() -> str | None:
    """返回一个能用的 python 解释器（优先带 cryptography 的）。"""
    override = _env(ENV_PYTHON)
    if override and Path(override).is_file():
        return override
    cands = [c for c in _python_candidates() if Path(c).is_file()]
    # PATH 上的 python / py
    for name in ("python.exe", "python3.exe"):
        p = shutil.which(name)
        # 跳过 WindowsApps 里的「应用执行别名」存根：它只是个转发器，
        # 未装商店版 Python 时一跑就弹商店，且 import cryptography 必然失败。
        if p and "\\WindowsApps\\" not in p:
            cands.append(p)
    # 优先挑能 import cryptography 的
    for c in cands:
        if _has_crypto(c):
            return c
    return cands[0] if cands else None


def _has_crypto(python: str) -> bool:
    try:
        r = subprocess.run([python, "-c", "import cryptography; print(cryptography.__version__)"],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def has_crypto(python: str) -> tuple[bool, str]:
    """返回 (是否有 cryptography, 版本或错误)。"""
    try:
        r = subprocess.run([python, "-c",
                            "import sys, cryptography; "
                            "print(f'{sys.version_info.major}.{sys.version_info.minor}|{cryptography.__version__}')"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return True, r.stdout.decode("utf-8", "replace").strip()
        return False, r.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:120]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:120]


# ── Codex 可执行文件 ────────────────────────────────────────
def find_codex() -> str | None:
    """返回 codex 的 .cmd/.exe 路径。

    有些环境 PATH 上排在前面的 codex 是 IDE 自带的构建，exec 会失败。所以默认优先
    npm 全局安装（用户自己装的那份），再退回 PATH。
    """
    override = _env(ENV_CODEX)
    if override:
        p = shutil.which(override) or (override if Path(override).is_file() else None)
        if p:
            return p
    appdata = os.environ.get("APPDATA", "")
    for rel in ("npm/codex.cmd", "npm/codex.exe", "npm/codex"):
        p = Path(appdata) / rel
        if p.is_file():
            return str(p)
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    return None


# ── 一次性体检 ──────────────────────────────────────────────
def diagnose(port: int = 8788) -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否通过, 说明)]，供 setup.py doctor 打印。"""
    out: list[tuple[str, bool, str]] = []

    # Python
    py = find_python()
    if py:
        ok, info = has_crypto(py)
        ver = info.split("|")[0] if "|" in info else "?"
        try:
            major, minor = (int(x) for x in ver.split(".")[:2])
            ver_ok = (major, minor) >= (3, 10)
        except ValueError:
            ver_ok = False
        out.append(("Python ≥3.10", ver_ok, py if ver_ok else f"{py}（版本 {ver} 偏低）"))
        out.append((f"cryptography", ok, info if ok else f"缺失 —— {info}"))
    else:
        out.append(("Python ≥3.10", False, "找不到 python，请安装 Python 3.10+ 并勾选 Add to PATH"))
        out.append(("cryptography", False, "跳过（没有 python）"))

    # WorkBuddy 桌面端
    app = find_app()
    out.append(("WorkBuddy 桌面端", bool(app), app or "找不到，请先安装 WorkBuddy AI 桌面端（国际版）"))

    # 凭据
    auths = find_auth_files()
    if auths:
        out.append(("登录凭据", True, auths[0] + (f"（另有 {len(auths)-1} 份备用）" if len(auths) > 1 else "")))
    else:
        out.append(("登录凭据", False, "找不到凭据文件，请先启动 WorkBuddy 桌面端并登录"))

    # Codex
    cx = find_codex()
    out.append(("Codex CLI", bool(cx), cx or "找不到 codex，请安装：npm i -g @openai/codex"))

    # 端口
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            busy = "已被占用（可能是本代理已在跑）"
            out.append((f"端口 {port}", True, busy))
    except OSError:
        out.append((f"端口 {port}", True, "空闲"))

    return out


def main() -> int:
    import argparse
    console_encodings()
    ap = argparse.ArgumentParser(description="路径与运行时自动发现")
    ap.add_argument("what", nargs="?", default="all",
                    choices=["app", "auth", "python", "codex", "all", "doctor"])
    ap.add_argument("--port", type=int, default=8788)
    a = ap.parse_args()

    if a.what == "app":
        print(find_app() or "")
    elif a.what == "auth":
        print(find_auth_file() or "")
    elif a.what == "python":
        print(find_python() or "")
    elif a.what == "codex":
        print(find_codex() or "")
    elif a.what == "doctor":
        width = max(len(n) for n, _, _ in diagnose(a.port))
        for name, ok, detail in diagnose(a.port):
            print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")
    else:
        print(f"app    : {find_app()}")
        print(f"auth   : {find_auth_file()}")
        print(f"python : {find_python()}")
        print(f"codex  : {find_codex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
