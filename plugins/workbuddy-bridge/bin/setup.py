"""workbuddy-bridge 安装 / 卸载器。

做四件事（全部幂等，可反复执行）：

  1. 在 $CODEX_HOME/config.toml 写入 [model_providers.wbbridge] 段
  2. 写入 $CODEX_HOME/wbb.config.toml（profile 本体；0.162 起 profile 必须是独立文件）
  3. 生成 $CODEX_HOME/wbbridge-catalog.json（模型切换列表的数据源）
  4. 安装 wbbridge-current-model 技能，并可选地在 AGENTS.md 里加一条「现查当前模型」规则

用法：
    python setup.py install      [--no-skill] [--no-agents-rule] [--port 8788]
    python setup.py uninstall    [--yes]
    python setup.py status
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wbspawn  # noqa: E402  （无控制台启动）
import wbdiscover  # noqa: E402
from wbdiscover import console_encodings  # noqa: E402

HERE = Path(__file__).resolve().parent          # .../bin
PKG_ROOT = HERE.parent                           # .../workbuddy-bridge
PROVIDER = "wbbridge"
PROFILE_NAME = "wbb"
MARK_BEGIN = "<!-- BEGIN workbuddy-bridge -->"
MARK_END = "<!-- END workbuddy-bridge -->"

PROVIDER_BLOCK = f"""[model_providers.{PROVIDER}]
name = "WorkBuddy"
base_url = "http://127.0.0.1:{{port}}/v1"
# 本地代理不校验密钥，占位即可（真正的登录态在代理里）
experimental_bearer_token = "wbbridge-local"
wire_api = "responses"
requires_openai_auth = false
# 上游会返回 429，给足退避重试次数，别让 Codex 很快放弃
request_max_retries = 6
stream_max_retries = 6
"""

PROFILE_BODY = f"""# Codex profile：走 WorkBuddy（本地代理）
#
# 用法：codex --profile {PROFILE_NAME}
# 前提：代理在跑 → scripts\\start-wbbridge.cmd（或 scripts\\codex-wbb.cmd 会自动拉起）
# provider 本体在 config.toml 的 [model_providers.{PROVIDER}]

model_provider = "{PROVIDER}"
model = "deepseek-v4.1-flash"
model_reasoning_effort = "low"
"""

AGENTS_RULE = f"""{MARK_BEGIN}
## 回答「当前模型」必须现查（不得凭上下文）

被问到「当前模型 / 现在用的什么模型 / which model / 换模型了吗」等身份类问题时：

**必须现场执行以下命令取数，禁止依据对话历史或记忆回答。**

```bash
curl -s http://127.0.0.1:{{port}}/wb/last-model
```

规则：

- 报告 `model` 字段（代理实际转发给上游的模型，唯一权威）。
- `config_model` 只是「新建会话的默认值」，不等于当前会话在用的模型。
- **模型无法内省自己的身份**：上下文里出现过的模型名、你自己上一轮的回答，全部不可信——
  用户可以在会话中途切换模型，历史消息里没有任何痕迹。
- 每次都重新执行上面的命令，不要复用本会话早先拿到的值。
{MARK_END}
"""


# ── 基础工具 ────────────────────────────────────────────────
def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def proxy_python() -> str:
    """挑一个带 cryptography 的 python；挑不到就用当前解释器。"""
    return wbdiscover.find_python() or sys.executable


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_proxy(port: int, wait: int = 25) -> bool:
    """代理没跑就拉起来，等到端口可用。"""
    if port_open(port):
        return True
    script = HERE / "wbproxy.py"
    if not script.is_file():
        print(f"  找不到 {script}")
        return False
    print(f"  启动代理：{script}")
    log = Path(os.environ.get("TEMP", ".")) / "wbbridge-proxy.log"
    try:
        # 必须用 wbspawn：venv 的 python.exe 是 uv 存根，转发时不传 creationflags，
        # 真实解释器会自己弹出一个可见控制台（实测 MainWindowHandle != 0）。
        wbspawn.spawn_detached([wbspawn.no_console_python(), str(script), "--port", str(port)],
                               log=log)
    except OSError as e:
        print(f"  启动失败：{e}")
        return False
    for _ in range(wait * 2):
        time.sleep(0.5)
        if port_open(port):
            return True
    print(f"  代理在 {wait} 秒内没有就绪，日志：{log}")
    return False


def backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    dst = path.with_name(path.name + time.strftime(".bak-wbbridge-%H%M%S"))
    shutil.copy2(path, dst)
    return dst


# ── 读写文本并保留原换行符 ──────────────────────────────────
# Codex 在 Windows 上写 CRLF。若用默认的文本模式读写，会把整份配置的换行符改成 LF，
# 表现为 diff 里「整个文件都变了」——必须按二进制原样读写。
def read_text_keep(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text_keep(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def to_newline(text: str, nl: str) -> str:
    """把文本统一成指定换行符（先把已有换行归一成 \\n 再替换）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


# ── TOML 段落的插入 / 移除（按文本行操作，保留其余内容与注释）──
def strip_section(text: str, header: str) -> str:
    """删掉名为 header 的 [section]（含其后续行，直到下一个 [ 或文件尾）。"""
    lines = text.splitlines(keepends=True)
    out, i, n = [], 0, len(lines)
    removed = False
    while i < n:
        if lines[i].strip() == header:
            removed = True
            i += 1
            while i < n and not lines[i].lstrip().startswith("["):
                i += 1
            # 连同上一行遗留的注释块也顺手让位于下一轮处理
            continue
        out.append(lines[i])
        i += 1
    return "".join(out) if removed else text


def upsert_section(text: str, header: str, body: str) -> str:
    """替换/追加一个 [section] 段。

    必须幂等：同一份输入反复执行，输出字节不变。上一版在追加前无条件补一个分隔空行，
    而 strip_section 不回收上次留下的那一行，于是每装一次就多一行空行（实测 159→160→161 行）。
    这里先把尾部空行清干净，再统一补「恰好一个」分隔空行。
    """
    nl = detect_newline(text)
    text = strip_section(text, header)
    head = text.rstrip("\r\n")
    body_out = to_newline(body, nl).rstrip("\r\n")
    if not head:
        return body_out + nl
    return head + nl + nl + body_out + nl


def upsert_top_key(text: str, key: str, value: str) -> str:
    """设置/追加**顶层**键（必须在第一个 [section] 之前）。

    Codex 只认顶层的 model_catalog_json；写进某个 section 里等于没写，
    桌面端模型列表就会回落到内置 OpenAI 目录（看不到任何 WorkBuddy 模型）。
    """
    nl = detect_newline(text) or "\n"
    lines = text.splitlines(keepends=True)
    # Windows 路径里全是反斜杠：TOML 的 "..." 基本字符串会把它当转义（`\U` 直接报错）。
    # 用 '...' 字面量字符串才安全（路径里不会出现单引号）。
    literal = "'" + value + "'" if "'" not in value else '"' + value.replace("\\", "\\\\") + '"'
    new_line = f"{key} = {literal}{nl}"
    insert_at = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            insert_at = i
            break
        if s.startswith(key + " ") or s.startswith(key + "="):
            lines[i] = new_line
            return "".join(lines)
    if insert_at == len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    # 后面紧跟 [section] 时补一个空行，读起来和手写的配置一致
    if insert_at < len(lines):
        new_line += nl
    lines.insert(insert_at, new_line)
    return "".join(lines)


# ── 各步骤 ──────────────────────────────────────────────────
def step_provider(home: Path, port: int) -> Path | None:
    cfg = home / "config.toml"
    if not cfg.is_file():
        print(f"  !! 找不到 {cfg}，请先安装并至少运行一次 Codex")
        return None
    bak = backup(cfg)
    text = read_text_keep(cfg)
    text = upsert_section(text, f"[model_providers.{PROVIDER}]", PROVIDER_BLOCK.format(port=port))
    write_text_keep(cfg, text)
    print(f"  已写入 [model_providers.{PROVIDER}]" + (f"（备份：{bak.name}）" if bak else ""))
    return cfg


def step_profile(home: Path) -> Path | None:
    prof = home / f"{PROFILE_NAME}.config.toml"
    if prof.is_file():
        bak = backup(prof)
        print(f"  已存在 {prof.name}" + (f"，备份：{bak.name}" if bak else ""))
    write_text_keep(prof, to_newline(PROFILE_BODY, "\r\n"))
    print(f"  已写入 profile：{prof.name}（用法：codex --profile {PROFILE_NAME}）")
    return prof


def step_catalog(home: Path, port: int, force_catalog: bool = False) -> Path | None:
    out = home / "wbbridge-catalog.json"
    gen = HERE / "gen-catalog.py"
    r = subprocess.run([proxy_python(), str(gen), "--port", str(port), "--out", str(out), "--quiet"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(f"  生成目录失败：{(r.stderr or r.stdout).strip()[:300]}")
        return None
    print(f"  已生成模型目录：{out.name}")

    # 目录生成了还得让 Codex 知道它在哪，否则桌面端看不到这些模型。
    cfg = home / "config.toml"
    if cfg.is_file():
        text = read_text_keep(cfg)
        if 'model_catalog_json' not in text:
            bak = backup(cfg)
            write_text_keep(cfg, upsert_top_key(text, "model_catalog_json", str(out)))
            print("  已在 config.toml 写入顶层 model_catalog_json"
                  + (f"（备份：{bak.name}）" if bak else ""))
        else:
            cur = ""
            for ln in text.splitlines():
                if ln.strip().startswith("model_catalog_json"):
                    cur = ln.split("=", 1)[1].strip().strip("'\"")
                    break
            if Path(cur) == out:
                print("  config.toml 的 model_catalog_json 已指向本插件目录")
            elif force_catalog:
                bak = backup(cfg)
                lines = []
                for ln in text.splitlines(keepends=True):
                    if ln.strip().startswith("model_catalog_json"):
                        nl = "\r\n" if ln.endswith("\r\n") else "\n"
                        ln = f"model_catalog_json = '{out}'{nl}"
                    lines.append(ln)
                write_text_keep(cfg, "".join(lines))
                print("  已把 model_catalog_json 改指本插件的目录"
                      + (f"（备份：{bak.name}）" if bak else ""))
            else:
                print(f"  !! config.toml 的 model_catalog_json 指向别处：{cur}")
                print("     它可能属于另一个同类插件。")
                print("     两边共存时模型列表只能指向其中一个；想让桌面端看到本插件的列表，")
                print(f"     手动把该行改成：model_catalog_json = '{out}'")
    return out


def step_skill(home: Path) -> None:
    src = PKG_ROOT / "skills" / "wbbridge-current-model"
    if not src.is_dir():
        print("  包里没有 skills/wbbridge-current-model，跳过")
        return
    dst = home / "skills" / "wbbridge-current-model"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  已安装技能：{dst}")


def step_agents_rule(home: Path, port: int) -> None:
    agents = home / "AGENTS.md"
    text = read_text_keep(agents) if agents.is_file() else ""
    nl = detect_newline(text) if text else "\r\n"
    rule = to_newline(AGENTS_RULE.format(port=port), nl).rstrip("\r\n")
    if MARK_BEGIN in text:                      # 已装过 → 先移除旧版再写，便于升级
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", text, flags=re.S)
    # 与 upsert_section 同理：先清掉上一轮留下的分隔空行，再补「恰好一个」。
    # 不清就会每装一次多一行空行（实测 65a66 / 66a67）。
    text = text.rstrip("\r\n")
    if text:
        text += nl + nl
    bak = backup(agents) if agents.is_file() else None
    write_text_keep(agents, text + rule + nl)
    print(f"  已在 AGENTS.md 加入「现查当前模型」规则" + (f"（备份：{bak.name}）" if bak else ""))


def step_watch(enable: bool) -> None:
    """把「按插件状态看护代理」装上登录自启。

    这是 hook 覆盖不到的部分：hook 只在会话开始时触发一次，管不了
    「会话进行中代理挂掉」「重启后还没开会话」「不用 Codex」这三种情况。
    """
    watcher = HERE / "watch_plugin.py"
    if not watcher.is_file():
        print("  跳过看护（找不到 bin/watch_plugin.py）")
        return
    exe = proxy_python()
    pyw = Path(exe).with_name("pythonw.exe")
    runner = str(pyw) if pyw.is_file() else exe
    action = "install-autostart" if enable else "remove-autostart"
    r = subprocess.run([exe, str(watcher), f"--{action}"],
                       capture_output=True, text=True, errors="replace")
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        print(f"  看护自启失败（退出码 {r.returncode}）：{(out or r.stderr or '').strip()[:200]}")
        print(f"  可手动重试：{runner} \"{watcher}\" --{action}")
        return
    if enable:
        print("  已安装代理看护自启：插件激活时代理自动在跑，未激活则自动停掉")
    else:
        print("  已移除代理看护自启")


def step_models_note(port: int) -> None:
    """列出可用模型，方便用户挑默认值。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=20) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  （读不到模型列表：{e}）")
        return
    ids = [m.get("id") for m in (doc.get("data") or []) if m.get("id")]
    free = {"hy3", "step-5-preview", "deepseek-v4.1-flash"}
    print(f"  可用模型 {len(ids)} 个；实测免费档：{', '.join(sorted(free))}")
    print(f"  改用默认模型：编辑 {codex_home() / (PROFILE_NAME + '.config.toml')} 的 model = \"...\"")


# ── 命令 ────────────────────────────────────────────────────
def cmd_install(args) -> int:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    print(f"安装到 {home}")
    print(f"包目录 {PKG_ROOT}")

    if not args.skip_doctor:
        print("\n[0/6] 环境体检")
        bad = [c for c in wbdiscover.diagnose(args.port) if not c[1]]
        for name, ok, detail in wbdiscover.diagnose(args.port):
            print(f"  [{'OK ' if ok else '!! '}] {name}: {detail}")
        # 代理没在跑不算致命（下一步会拉起）；其它失败要先解决
        fatal = [c for c in bad if "端口" not in c[0]]
        if fatal:
            print("\n  上面标 !! 的项需要先解决。修好后重跑 install.cmd。")
            if not args.force:
                return 1

    print("\n[1/6] 确保本地代理可用（生成目录需要它）")
    if not ensure_proxy(args.port):
        print("  代理不可用。请先启动并登录 WorkBuddy AI 桌面端，再重试。")
        return 1
    print("  OK")

    print("\n[2/6] 写入 provider")
    if not step_provider(home, args.port):
        return 1

    print("\n[3/6] 写入 profile")
    step_profile(home)

    print("\n[4/6] 生成模型目录")
    step_catalog(home, args.port, args.take_catalog)

    print("\n[5/6] 附加项")
    if args.no_skill:
        print("  跳过技能安装（--no-skill）")
    else:
        step_skill(home)
    if args.no_agents_rule:
        print("  跳过 AGENTS.md 规则（--no-agents-rule）")
    else:
        step_agents_rule(home, args.port)

    step_models_note(args.port)

    if args.no_watch:
        print("  跳过代理看护自启（--no-watch）")
    else:
        step_watch(True)

    print("\n完成。用法：")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wbb.cmd'}                 # TUI（自动拉起代理）")
    print(f"  {PKG_ROOT / 'scripts' / 'codex-wbb.cmd'} exec \"任务\"      # 非交互")
    print(f"  codex --profile {PROFILE_NAME}                            # 已装 Codex 时")
    print("\n注意：桌面端切换模型不会写回 config.toml，代理会自动同步；")
    print("      旧的会话记住自己创建时的模型，改用「新聊天」才走新默认值。")
    return 0


def cmd_uninstall(args) -> int:
    home = codex_home()
    print(f"从 {home} 卸载")
    cfg = home / "config.toml"
    if cfg.is_file():
        bak = backup(cfg)
        text = strip_section(read_text_keep(cfg), f"[model_providers.{PROVIDER}]")
        # 段被删掉后常留下连续空行，收成「末尾一个换行」
        nl = detect_newline(text) or "\n"
        text = re.sub(r"(\r?\n){3,}", nl + nl, text).rstrip("\r\n") + nl
        write_text_keep(cfg, text)
        print(f"  已移除 [model_providers.{PROVIDER}]" + (f"（备份：{bak.name}）" if bak else ""))

    prof = home / f"{PROFILE_NAME}.config.toml"
    if prof.is_file():
        prof.unlink()
        print(f"  已删除 {prof.name}")

    cat = home / "wbbridge-catalog.json"
    if cfg.is_file():
        cur = read_text_keep(cfg)
        if "wbbridge-catalog.json" in cur:
            bak = backup(cfg)
            kept = [ln for ln in cur.splitlines(keepends=True)
                    if not (ln.strip().startswith("model_catalog_json")
                            and "wbbridge-catalog.json" in ln)]
            write_text_keep(cfg, "".join(kept))
            print("  已移除 config.toml 的 model_catalog_json"
                  + (f"（备份：{bak.name}）" if bak else ""))
    if cat.is_file():
        cat.unlink()
        print(f"  已删除 {cat.name}")

    dst = home / "skills" / "wbbridge-current-model"
    if dst.is_dir():
        shutil.rmtree(dst)
        print(f"  已删除技能 {dst}")

    agents = home / "AGENTS.md"
    if agents.is_file() and MARK_BEGIN in read_text_keep(agents):
        bak = backup(agents)
        cur = read_text_keep(agents)
        nl = detect_newline(cur)
        text = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\r?\n?",
                      "", cur, flags=re.S)
        write_text_keep(agents, text.rstrip("\r\n") + nl)
        print(f"  已移除 AGENTS.md 规则" + (f"（备份：{bak.name}）" if bak else ""))

    step_watch(False)

    print("\n完成。代理进程若还在跑，可手动结束；包目录未删除。")
    return 0


def cmd_doctor(args) -> int:
    """环境体检：不安装任何东西，只报告缺什么、怎么补。"""
    home = codex_home()
    print(f"CODEX_HOME : {home}")
    print(f"包目录      : {PKG_ROOT}")
    print()
    rows = wbdiscover.diagnose(args.port)
    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"  [{'OK ' if ok else '!! '}] {name:<{width}}  {detail}")

    bad = [r for r in rows if not r[1] and "端口" not in r[0]]
    print()
    if bad:
        print("需要处理：")
        for name, _, _ in bad:
            tip = {
                "Python ≥3.10": "装 Python 3.10+（安装时勾选 Add python.exe to PATH），或设 WBBRIDGE_PYTHON",
                "cryptography": '用上一步的 python 执行：python -m pip install cryptography',
                "WorkBuddy 桌面端": "装 WorkBuddy AI 桌面端（国际版）并登录；或用 WBBRIDGE_APP 指定路径",
                "登录凭据": "启动桌面端并完成登录，凭据会自动写出",
                "Codex CLI": "npm i -g @openai/codex，或用 WBBRIDGE_CODEX 指定",
            }.get(name, "")
            if tip:
                print(f"  - {name}：{tip}")
        return 1
    print("环境就绪，可以运行 install.cmd")
    return 0


def cmd_status(args) -> int:
    home = codex_home()
    cfg = home / "config.toml"
    text = read_text_keep(cfg) if cfg.is_file() else ""
    print(f"CODEX_HOME          : {home}")
    print(f"provider 段已安装    : {f'[model_providers.{PROVIDER}]' in text}")
    print(f"profile 文件        : {(home / (PROFILE_NAME + '.config.toml')).is_file()}")
    print(f"模型目录            : {(home / 'wbbridge-catalog.json').is_file()}")
    print(f"技能                : {(home / 'skills' / 'wbbridge-current-model').is_dir()}")
    ag = home / "AGENTS.md"
    print(f"AGENTS.md 规则      : {ag.is_file() and MARK_BEGIN in read_text_keep(ag)}")
    print(f"代理 {args.port} 在跑 : {port_open(args.port)}")
    startup = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup" / "workbuddy-bridge-watch.vbs")
    print(f"代理看护自启   : {startup.is_file()}")
    if port_open(args.port):
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/wb/last-model", timeout=10) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            print(f"  实际在用模型      : {d.get('model')}")
            print(f"  配置默认          : {d.get('config_model')} (synced={d.get('config_synced')})")
        except Exception as e:
            print(f"  （查询失败：{e}）")
    return 0


def main() -> int:
    console_encodings()
    ap = argparse.ArgumentParser(description="workbuddy-bridge 安装器")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WBBRIDGE_PROXY_PORT", 8788)))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="安装（幂等）")
    p.add_argument("--no-skill", action="store_true", help="不安装 wbbridge-current-model 技能")
    p.add_argument("--no-agents-rule", action="store_true", help="不改动全局 AGENTS.md")
    p.add_argument("--skip-doctor", action="store_true", help="跳过环境体检")
    p.add_argument("--force", action="store_true", help="体检查出问题时也继续安装")
    p.add_argument("--no-watch", action="store_true", help="不安装代理看护自启")
    p.add_argument("--take-catalog", action="store_true",
                   help="把 config.toml 的 model_catalog_json 改指本插件（已有其它指向时默认不动）")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="卸载（保留包目录与备份）")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("status", help="查看安装状态与实际在用模型")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="环境体检（不安装任何东西）")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
