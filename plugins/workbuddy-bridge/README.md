# workbuddy-bridge

把 **WorkBuddy AI 桌面端**已登录的模型，变成 **Codex（CLI / 桌面端）**的模型源。

不需要 OpenAI 订阅，不需要 Codex 套餐——Codex 只用它的 agent 骨架（读写文件、跑命令、apply_patch、
多轮工具循环），模型换成 WorkBuddy 的（GPT-6 / GPT-5.6 / GLM / Kimi / Gemini / Grok / Hy3 …），
其中有 **`hy3`、`deepseek-v4.1-flash`、`step-5-preview` 三个实测 0 扣分**的免费档。

```
Codex ──► 127.0.0.1:8788（本机代理）──► workbuddy.ai ──► 真实模型
           │  解密桌面端登录态
           │  Responses ⇄ Chat Completions 转换
           │  记录/同步「实际在用」的模型
           │  生成模型切换列表
```

---

## 作为 Codex 插件安装

本包是一个 **Codex plugin**，装在 marketplace 仓库的 `plugins/` 下。装上插件只是把文件铺好，
**还需要跑一次安装器**把 provider 写进 Codex 配置。

```cmd
:: 1. 加市场（从 GitHub 装；本地目录/zip 换成对应路径）
codex plugin marketplace add zetongliu219-alt/workbuddy-bridge

:: 2. 装插件
codex plugin add workbuddy-bridge@wbbridge

:: 3. 找到插件真实位置（Codex 会把它物化到 cache）
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"

:: 4. 体检 + 安装（幂等）
python "<插件根>\bin\setup.py" doctor
python "<插件根>\bin\setup.py" install
```

第 4 步也可以让 Codex 自己跑：本插件带了 **`/wbbridge-setup`** 与 **`/wbbridge-status`** 命令。
**不过在 Codex 0.153 上实测这两个不会被暴露**（`plugin/read` 不枚举 commands），
所以以直接跑脚本为准 —— 命令当作对更新版本的便利入口。

本插件提供三个面：

| 面 | 内容 |
|---|---|
| `skills/` | `workbuddy-bridge`（用法/排错）、`wbbridge-current-model`（正确回答「当前模型」） |
| `commands/` | `/wbbridge-setup`、`/wbbridge-status` |
| `hooks/hooks.json` | `SessionStart`：代理没跑就自动拉起 |

### hook 需要信任一次（**必须做，否则不会自动启动代理**）

Codex 默认不让 hook 自动执行。**未信任时 hook 状态是 `untrusted`，它永远不会跑**，
症状就是「代理没起来 → 桌面端静默连不上」。查当前状态：

```cmd
:: 起 app-server 问一句，或直接看下面「桌面端点法」的结果
```

**桌面端点法**：打开 **设置 → Hooks**。这个页面的存在与文案从 app 包里确认过：
`settings.nav.hooks`、副标题 *"Manage lifecycle hooks from config and enabled plugins"*、
来源分组里有 **From Plugins**，未信任的 hook 显示 **"Disabled until hook is trusted"**。
找到本插件那条（`SessionStart`），点 **Trust all**。

**TUI 点法**：新开会话时会弹

> Hooks need review — Hooks can run outside the sandbox after you trust them.
> [Trust all and continue] [Continue without trusting (hooks won't run)]

选 **Trust all and continue**。

**自动化 / 不想点**：`codex exec --dangerously-bypass-hook-trust ...`（等同于每个调用都绕过信任）。

信任状态存在 `config.toml` 的 `hooks.state.<hook key>.trusted_hash`（`<hook key>` 形如
`workbuddy-bridge@wbbridge:hooks/hooks.json:session_start:0:0`）。
**改过 hook 内容后会被判定为「Hook changed since last trusted」，需要重新信任。**
如果显示「Another config layer overrides these hook settings」，说明有更高优先级的配置层
（admin/项目配置）压着，要先处理那一层。

### 两层保障：hook（会话级）+ 看护（常驻）

只靠 hook 有三个洞，因为 `SessionStart` 的语义是 **“When a new session starts”**：

| 场景 | 只有 hook | 装了看护后 |
|---|---|---|
| 新开 CLI 会话 / 桌面端新聊天 | ✅ | ✅ |
| 机器重启后第一次开会话 | ✅ | ✅ |
| **已开着的会话里代理中途挂了** | ❌ 不会自愈 | ✅ 几秒内自动恢复 |
| **不用 Codex、只想代理常驻** | ❌ | ✅ |
| **在 Codex 里停用插件** | ❌ 代理还在跑 | ✅ 自动停掉代理 |

看护（`bin/watch_plugin.py`）就是**按插件启用状态开关代理**：

- **插件激活**（`config.toml` 里 `[plugins."workbuddy-bridge@wbbridge"] enabled = true`
  且插件已安装）→ 代理保持在跑
- **插件未激活**（`enabled = false`，或被卸载）→ 停掉代理

`install.cmd` 会把它装成**登录自启**（写一个 VBS 到启动文件夹，免管理员权限）。
停用插件就等于关代理，启用就等于开 —— 不需要单独管代理。

看护命令：

```cmd
<插件>\scripts\watch-bridge.cmd --status            当前判定（激活？代理在跑？是否一致）
<插件>\scripts\watch-bridge.cmd --once              只对账一次
<插件>\scripts\watch-bridge.cmd                     常驻看护（Ctrl+C 退出）
<插件>\scripts\watch-bridge.cmd --install-autostart 装登录自启
<插件>\scripts\watch-bridge.cmd --remove-autostart  取消登录自启
```

日志：`%TEMP%\wbbridge-watch.log`。只记状态变化与错误，正常轮询不写日志。

安全性：停代理前会核对进程命令行是否含本包的 `wbproxy.py`，
**端口上若有别人的程序，绝不误杀**；看护自身有单实例锁。

> **写 hook 命令的两条硬约束**（都是实测出来的，改之前先看）：
> 1. 必须用 `%CLAUDE_PLUGIN_ROOT%` 拼**绝对**路径 —— hook 的 cwd 是会话目录而非插件根，
>    相对路径（`./scripts/x.cmd`、`scripts\x.cmd`）一律失败。
> 2. 必须保持 **stdout 空白** —— Codex 会把 hook 的 stdout 当 JSON 解析，打了中文就会判
>    hook Failed（哪怕脚本其实干活成功了）。诊断信息写日志文件
>    （`%TEMP%\wbbridge-hook.log`）。

### 重装 / 更新：完整流程（实测可跑）

Codex 按**版本号**把插件**复制**到 `plugins/cache/<市场>/<插件>/<版本>/`，
所以「改了代码」或「要修状态」都必须重装一次，不能指望它读源目录。

**第 0 步 · 先停掉会占用缓存目录的进程**（否则重装报
`failed to back up plugin cache entry: 拒绝访问 (os error 5)`）—— 看护和代理都会占用：

```cmd
:: 只按精确 PID 结束，别用名字/命令行匹配（会误伤别的程序，甚至杀掉执行命令的 shell）
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'wbproxy|watch_plugin' } | ForEach-Object { $_.ProcessId }"
:: 把上面列出的 PID 逐个结束：
taskkill /F /PID <PID>
```

**第 1 步 · 确认市场已注册**（只需注册一次，重装不必重复）：

```cmd
codex plugin marketplace list
:: 没有 workbuddy 时才需要：
:: codex plugin marketplace add zetongliu219-alt/workbuddy-bridge
```

**第 2 步 · 装/重装插件**：

```cmd
codex plugin add workbuddy-bridge@wbbridge
```

> 若**版本号没变**，Codex 会直接用已有缓存，改了的内容不生效。
> 本地开发时先升版本：
> ```cmd
> python "<插件目录>\bin\bump-plugin.py" "<插件目录>"
> ```
> 从 GitHub 拉最新则是先 `git pull`（版本号已在仓库里升好）。

**第 3 步 · 跑插件自带的安装器**（写 provider / profile / 模型目录 / 技能 / AGENTS 规则，
并装看护自启）：

```cmd
:: 找到刚装出来的真实目录（含版本号）
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"

python "<插件目录>\bin\setup.py" doctor        :: 体检
python "<插件目录>\bin\setup.py" install       :: 安装（幂等）
```

**第 4 步 · 核对**：

```cmd
codex plugin list -m wbbridge                   :: 应显示 installed, enabled + 版本号
python "<插件目录>\bin\setup.py" status         :: 各项 True
python "<插件目录>\bin\watch_plugin.py" --status   :: 插件激活 / 代理在跑 / 一致
curl -s http://127.0.0.1:8788/health              :: ok:true
```

几条容易踩的：

- **`codex plugin list` 显示 `not installed` 但目录还在** → 说明配置里的
  `[plugins."workbuddy-bridge@wbbridge"]` 条目丢了，重跑第 2 步；不必重新 `marketplace add`。
- **重装后看护"不工作"** → 看护是从**旧版本缓存目录**启动的（旧目录一删就悬空）。
  重装后按第 3 步跑一次 `install`，它会把自启重新指向新版本目录。
- **控制台窗口** → 若看到 `…\venv\Scripts\python.exe` 的窗口，是 1.5.2 之前版本的 bug，
  升级到 1.5.3+ 即可（详见下文「后台进程不显示控制台窗口」）。
- **hook 信任状态**在重装后**会保留**（存在 `config.toml` 的 `hooks.state.*`），
  一般不需要重新信任；若 hook 内容变了则需重新 Trust。

---


## 目录结构

```
workbuddy-bridge/
├─ install.cmd / uninstall.cmd / status.cmd / doctor.cmd   装 / 卸 / 查 / 体检
├─ .codex-plugin/plugin.json    插件清单（Codex plugin 的必需文件）
├─ bin/
│  ├─ wbproxy.py        本地代理：凭据解密、请求改写、SSE 回译、模型同步
│  ├─ wbresponses.py    Responses API ⇄ Chat Completions 适配层
│  ├─ wbdiscover.py     路径与运行时自动发现（桌面端/Python/Codex/凭据）
│  ├─ gen-catalog.py    生成 Codex 的模型切换列表（wbbridge-catalog.json）
│  ├─ hook_session_start.py  SessionStart hook：确保代理在跑
│  ├─ bump-plugin.py    更新插件时打 cachebuster 版本号
│  ├─ watch_plugin.py   按插件启用状态看护代理（激活则起，未激活则停）
│  ├─ wbspawn.py        以后台、无控制台窗口的方式启动 Python 进程
│  └─ setup.py          安装/卸载器（写 provider、profile、目录、技能、指令规则）
├─ commands/            /wbbridge-setup、/wbbridge-status
├─ hooks/hooks.json     SessionStart（需在 Codex 里信任一次才自动执行）
├─ scripts/
│  ├─ codex-wbb.cmd      启动器：代理没跑就自动拉起，再以 wbb profile 起 Codex
│  ├─ start-wbbridge.cmd 只启动代理
│  ├─ hook-session-start.cmd  hook 入口（必须静默 + 用 %CLAUDE_PLUGIN_ROOT%）
│  └─ _findpy.cmd       被上面几个 call：找出可用的 python
├─ skills/
│  ├─ workbuddy-bridge/   用法、免费档、排错、合规
│  └─ wbbridge-current-model/       让 Codex 正确回答「当前模型」
├─ templates/                    参考片段（免安装时手工照抄用）
└─ dev/                          [本地] 诊断/测试脚本（**不随仓库发布**）
```

---

## 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| **Windows** | — | 依赖 `CREATE_NO_WINDOW`、`%LOCALAPPDATA%`、直接调 `.exe`，非跨平台代码 |
| **WorkBuddy AI 桌面端** | 已安装**且已登录** | 凭据来源。**不需要常驻运行**，装过登录过即可 |
| **Python** | 3.10+，含 `cryptography` | 用于 AES-256-GCM 解密。`install.cmd` 会自动挑一个带 `cryptography` 的解释器 |
| **Codex** | 0.153+ | 见下文「版本」一节 |

上表四项**全部自动探测**，不需要改任何路径。先跑一次体检可以确认：

```cmd
doctor.cmd
```

```
  [OK ] Python ≥3.10     C:\...\python.exe
  [OK ] cryptography     3.11|50.0.0
  [OK ] WorkBuddy 桌面端  D:\...\WorkBuddyAI.exe
  [OK ] 登录凭据          C:\...\workbuddy-desktop-ai.info（另有 1 份备用）
  [OK ] Codex CLI        C:\...\npm\codex.cmd
  [OK ] 端口 8788         空闲
```

桌面端装在哪个盘都行（探测顺序：环境变量 → 注册表卸载项 → 正在运行的进程 → 常见安装路径）。

---

## 安装

```cmd
install.cmd
```

它会做（**每一步都幂等**，反复运行不会改动已就绪的内容）：

0. 体检（Python / cryptography / 桌面端 / 凭据 / Codex / 端口），有问题会列出来并给出修法
1. 确保代理可用（不可用就拉起来）——生成目录需要它
2. 往 `<CODEX_HOME>\config.toml` 写入 `[model_providers.wbbridge]`
3. 写入 `<CODEX_HOME>\wbb.config.toml`（profile）
4. 生成 `<CODEX_HOME>\wbbridge-catalog.json`（模型切换列表的数据源）
5. 安装 `wbbridge-current-model` 技能；往 `AGENTS.md` 加一条「现查当前模型」规则

`CODEX_HOME` 默认 `%USERPROFILE%\.codex`。每一步改文件前都会备份（`.bak-wbbridge-<时刻>`）。
跳过附加项 / 跳过体检：

```cmd
install.cmd --no-skill --no-agents-rule
install.cmd --skip-doctor
```

查状态：

```cmd
status.cmd
```

### 与其它同类接入共存

本包用独立的一套标识：端口 `8788`、provider `wbbridge`、profile `wbb`、
缓存目录 `plugins/cache/wbbridge/`、看护任务 `workbuddy-bridge-watch`。
因此机器上若还装着别的 WorkBuddy 接入插件，两者可以同时装、同时跑，互不干扰。

唯一会「撞」的是 `config.toml` 的顶层 `model_catalog_json` —— 它只有一个值，
桌面端模型列表只能显示其中一个插件的目录。默认行为：

- 该键**不存在** → 安装器写入本插件的目录
- 该键**已存在且指向别处** → 安装器只提示，不改动（避免抢走别人的列表）
- 想强制改指本插件 → `python setup.py install --take-catalog`

---

## 装到别人机器上

**推荐走插件安装**（见上文「作为 Codex 插件安装」）：把本仓库（marketplace 形态）拷过去或推到
GitHub，对方 `codex plugin marketplace add ...` + `codex plugin add workbuddy-bridge@wbbridge`，
再跑一次 `bin\setup.py install`。

也可以**直接把插件目录拷过去**用，不经 Codex 的插件机制 —— 因为**没有需要改的路径**：
桌面端装在哪个盘、Python 装在哪里、Codex 是 npm 还是 pnpm 装的，都由 `bin/wbdiscover.py`
现场探测，启动脚本也共用同一套逻辑。

收件人要做的事：

1. 装 **WorkBuddy AI 桌面端（国际版）**，登录一次
2. 装 **Python 3.10+**（勾选 Add to PATH），然后
   `python -m pip install cryptography`
3. 装 **Codex CLI**（`npm i -g @openai/codex`）
4. 跑 `doctor.cmd`，全 OK 后跑 `install.cmd`

**凭据是 per-machine / per-account 的**：包本身不含任何账号信息，也不含令牌。
每个用户必须自己装桌面端、自己登录，凭据由桌面端写在自己机器上
（`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`）。

探测不到或想强制指定时，用环境变量覆盖（都不设 = 全自动）：

| 变量 | 作用 |
|---|---|
| `WBBRIDGE_APP` | `WorkBuddyAI.exe` 全路径 |
| `WBBRIDGE_AUTH_FILE` | 凭据文件全路径 |
| `WBBRIDGE_PYTHON` | 指定 python 解释器 |
| `WBBRIDGE_CODEX` | 指定 `codex-wbb.cmd` 用哪个 codex |

> 国内版（`workbuddy-desktop.info`，CodeBuddy）与本包的上游地址不同，**未做适配**。
> 两份凭据同时存在时优先取国际版 `…-ai.info`。`doctor.cmd` 会显示用的是哪一份。

---

## 用法

```cmd
scripts\codex-wbb.cmd                     交互式 TUI（代理没跑会自动拉起）
scripts\codex-wbb.cmd exec "重构 utils.py"  非交互
scripts\codex-wbb.cmd -m hy3              换模型
codex --profile wbb                       已装 Codex 时直接用
```

桌面端图形界面同样可用：**必须新建会话**（详见下文「模型与会话」）。

---

## 模型与计费

实测费率（同规模请求，约 2–3 万输入 token）：

| 模型 | 扣分 | 每千输入 | 备注 |
|---|---|---|---|
| `hy3` | **0** | 0 | 免费，192k 上下文 / 64k 输出 |
| `step-5-preview` | **0** | 0 | 免费，300k / 64k |
| `deepseek-v4.1-flash` | **0** | 0 | **免费，300k / 128k（推荐默认）** |
| `glm-5.3-flash` | 0.12 | 0.0050 | |
| `gpt-6-luna` | 0.38 | 0.0127 | |
| `gpt-6-sol` | 7.51 | 0.2502 | **比 luna 贵 20 倍**，别当默认 |

> ⚠️ `hy4-preview` 名字带 preview 但**收费**（0.02）。**不要凭名字判断免费**，以实测为准。

扣分写在响应的 `usage.credit` 字段；代理把它记到 `%TEMP%\wbbridge-usage.jsonl`，也汇总在
`GET /wb/credits`（账号剩余积分）。

---

## 代理提供的端点

| 端点 | 用途 |
|---|---|
| `GET /health` | 账号 uid、令牌到期、`last_model` / `last_model_ts` / `requests_seen` |
| `GET /wb/last-model` | **实际在用**的模型（权威）+ 配置里的默认值 + 是否一致 |
| `GET /wb/credits` | 账号剩余积分 |
| `GET /v1/models` | 模型目录（同时给 OpenAI 形状 `data` 和 Codex 形状 `models`） |
| `POST /v1/chat/completions` | OpenAI 兼容（任意 OpenAI 客户端） |
| `POST /v1/responses` | Responses API（Codex 0.153+ 唯一支持的形式） |

### 模型状态如何保持一致

Codex 里有两层模型值，**极易误判**：

| | 含义 | 存放位置 |
|---|---|---|
| `config.toml` 的 `model` | **新建会话的默认值** | 磁盘 |
| 选择器里选的 | **当前会话的运行时覆盖** | 只在内存，**不写回配置** |

后果有两个，都已解决：

1. **配置滞后** → 代理每次转发请求时，把顶层 `model` 同步回 `config.toml`（只改那一行，
   原子替换）。`WBBRIDGE_SYNC_CONFIG=0` 可关。
2. **模型答错自己的身份** → 模型无法内省，只能读文件或复述上下文里的旧答案。安装器往
   `AGENTS.md` 加了一条硬规则：被问当前模型时**必须现跑**
   `curl -s http://127.0.0.1:8788/wb/last-model`，禁止凭记忆回答。

> 代价：配置默认会跟随「最后一次用过的模型」。临时用了 `gpt-6-sol` 之类，之后新建会话就会
> 默认走它——要固定默认值就设 `WBBRIDGE_SYNC_CONFIG=0` 或在选择器切回来。

---

## 已知坑（都是踩过的）

### 上游的硬约束（不可绕过，只能适配）

| 现象 | 原因 | 代理怎么处理 |
|---|---|---|
| `400 code 11101` | 上游不支持非流式 | 强制 `stream:true`，本地聚合后再回给客户端 |
| `400 code 11128` | **首条必须是 `system`**；且**完全不接受 `developer` 角色** | 把 developer 折回 system |
| `400 code 11128` | **按 system prompt 指纹认客户端**。Codex 的身份段会触发；注意这**不是关键词黑名单**：三句具体措辞**连起来**才触发，单句分别发都通过 | 只把开头约 800 字的身份段替换成中性描述，其余 3 万字行为指令原样保留 |

### Codex 侧

- **`wire_api = "chat"` 在 0.153 起被移除**，只认 `responses`。这就是需要 `wbresponses.py` 的原因。
- **模型目录 schema**：Codex 要 `{"models":[...]}`，条目字段是 `slug` / `display_name` /
  `base_instructions` / `tool_mode` / `apply_patch_tool_type` …，与 OpenAI 形状完全不同。
  缺 `tool_mode` 等字段时，**0.16x 会认为「没有可用工具」而发 0 个工具**，模型就会说
  「没有可用的执行工具」。`gen-catalog.py` 已补齐。
- **profile 必须是独立文件**（0.162 起）：写在 `config.toml` 里的 `[profiles.x]` 会被判为
  legacy 并**直接报错**。所以 profile 是 `wbb.config.toml`。
- **版本要固定**：有些环境里 `PATH` 上排在前面的 codex 是某个 IDE 自带的构建，`exec` 会失败
  （`Failed to create unified exec process: helper_unknown_error: setup refresh had errors`）。
  `codex-wbb.cmd` 因此**优先用 npm 全局安装的那份**；`WBBRIDGE_CODEX=codex` 可改回 PATH。

### Windows 脚本

- **`.cmd` 必须 GBK(cp936) + CRLF**。写成 UTF-8 时，中文在 936 代码页下会**吃掉行尾换行**，
  把下一行并进 `rem`，报出 `'obreak' 不是内部或外部命令` 这类莫名其妙的错误。
- 脚本里调 `curl` / `ping` 要用 `%SystemRoot%\System32\` 全路径，否则 git-bash 的 GNU 版本会
  抢先匹配（报 `invalid time interval '/t'`）；等待也**不要用 `timeout.exe`**（stdin 被重定向时
  直接报错），用 `ping -n 2 127.0.0.1 >NUL`。
- **PowerShell 脚本必须纯 ASCII**：5.1 按 ANSI/代码页读 `.ps1`，UTF-8 中文注释会变乱码并
  导致**解析失败**。

### 限流（429）

**是上游应用层的账号级限流，不是 CDN/WAF 拦截。** 完整响应体（直连上游抓的）：

```json
{
  "code": 14003,
  "msg": "too many requests",
  "retry_policy": { "retry": true },
  "displayMsg":  { "zh": "请求过于频繁，请稍后重试。" },
  "displayTips": { "zh": "建议等待约 1 分钟后重试，或切换到负载较低的模型。" }
}
```

要点：

- **官方给了处置办法**：等约 1 分钟，或**换一个负载较低的模型**（换模型确实能绕过——实测有效）。
- **瞬时性**：同一客户端连续请求会出现 429/200 交替（实测 1→429、2→200、3→429）。
- **不计费**：429 的请求不进 usage，账号积分不变。
- **无法按需复现**：10 路并发 × 30 次 ≈ 90 万 token / 35 秒的压力测试**没触发**；
  它是按账号整体活跃度算的，压测打不出来。
- **相关因素**：**同一账号被多个客户端共用**。WorkBuddy 桌面端在跑时（启动会刷新令牌、
  同步目录、拉会话）与 Codex 走代理的流量叠加，最容易触发。
- 响应头里有 `Server: TencentEdgeOne` + `X-WAF-UUID`（流量都过 CDN），
  但**没有任何 `X-RateLimit-*`**——限额不可见，只能撞到。

缓解：

- provider 里已设 `request_max_retries = 6` / `stream_max_retries = 6`，让 Codex 多退避几次。
- 撞到就**等一分钟**，或换模型（`hy3` / `deepseek-v4.1-flash` 之间来回切）。
- 别让桌面端和 Codex 同时跑 agent 任务。

**排查方式**：代理会把上游错误连同响应体记进日志（`上游错误 429: {...}`）。这个日志原本
没有，是靠它才定位到 `code 14003` 的——所以三处 `except UpstreamError` 都加了记录。

### 桌面端 GUI

- 模型切换**只对新会话生效**：已有会话记住创建时的 provider/model，旧会话会继续走旧的
  （表现为一直报上游错误）。**改用「新聊天」**。
- 桌面端用的 codex 是它自带的（`~/.codex/bin/<hash>/codex.exe`），**与 CLI 那份不同**，
  换二进制要分别验证。

---

## 故障排查

| 症状 | 原因 / 处理 |
|---|---|
| `wire_api = "chat" is no longer supported` | Codex 版本太新，必须走 `responses`（本包已实现） |
| `400 code 11128 Illegal API invocation` | 上游指纹拦截。确认用的是本包代理（含 `sanitize_system`） |
| 模型说「没有可用的执行工具」 | 模型目录缺 `tool_mode` 等字段 → 重跑 `gen-catalog.py` |
| 桌面端模型列表只有 `gpt-6-*` / `daybreak-*` | `model_catalog_json` 没设或文件不存在 → 重跑安装 |
| `--profile wbb cannot be used ... legacy [profiles.wb]` | profile 写进了 `config.toml`，应放 `wbb.config.toml` |
| 一直「正在重新连接 N/N」 | 看错误里的 url。多半是**上游/中转的错**（例：中转返回 402 无有效订单），不是网络 |
| `exceeded retry limit, last status: 429` | 上游边缘限流，见上文。放慢节奏，别同时跑桌面端 agent |
| 问「当前模型」答错 | 模型没重新取数。确认 `AGENTS.md` 规则已装，或直接查 `/wb/last-model` |
| 代理起不来 | WorkBuddy 桌面端未装 / 未登录；或凭据文件位置变了（见下） |

### 日志与排查

代理默认**只记错误类事件**（上游报错、令牌刷新、凭据问题）——这些是排查的刚需，
不需要装什么就能看到：

```
[wbproxy] 上游错误 429: code=14003 / too many requests / 建议等待约 1 分钟后重试，或切换到负载较低的模型。 / requestId=...
```

加 `--verbose` 才记请求流水（每次请求的模型、消息数、工具数）。看请求流水：

```cmd
scripts\start-wbbridge.cmd --verbose
```

**客户端断线是常态**（用户取消、429 重试途中放弃、切会话），代理会记一行
`连接中断（ConnectionResetError），忽略` 然后继续服务，不会打堆栈、不会退出。
Windows 上这类异常是 `ConnectionAbortedError`（WinError 10053），与 Linux 的
`BrokenPipeError` 不同名——只捕后者会漏。

凭据文件由桌面端写在 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`
（国际版 `workbuddy-desktop-ai.info`，国内版 `workbuddy-desktop.info`）。
**自动探测**，可用 `WBBRIDGE_AUTH_FILE` / `WBBRIDGE_APP` 覆盖。
实测这两处最容易变：桌面端升级后绑定名或文件名可能改，症状是代理起不来或解密失败。

### 限流来得比想象中快

实测连续 8 次请求就是 `200, 429, 200, 429…` 交替——**账号级限流按整体活跃度算，
不看你打得多快**。429 不计费，等约 1 分钟或换模型即可。
一次 Codex agent 任务约 5k–100k token、数轮工具调用，正常用没问题；
但别把它当免费 API 批量跑（同样会连累桌面端自己也报限流）。

---

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `WBBRIDGE_PROXY_PORT` | `8788` | 代理端口 |
| `WBBRIDGE_PROXY_HOST` | `127.0.0.1` | 监听地址（**别改成 0.0.0.0**，见安全） |
| `WBBRIDGE_AUTH_FILE` | 自动探测 | 凭据文件路径 |
| `WBBRIDGE_APP` | 自动探测 | `WorkBuddyAI.exe` 路径 |
| `WBBRIDGE_PYTHON` | 自动探测 | 启动脚本用哪个 python |
| `WBBRIDGE_SYNC_CONFIG` | `1` | 关掉配置同步 |
| `WBBRIDGE_CODEX_CONFIG` | `$CODEX_HOME/config.toml` | 要同步的配置文件 |
| `WBBRIDGE_USAGE_LOG` | 空 | 记 usage/积分的 jsonl 路径 |
| `WBBRIDGE_CODEX` | npm 全局 codex | 指定启动器用哪个 codex |
| `WBBRIDGE_MODEL` | `deepseek-v4.1-flash` | 本地 `dev/` 脚本用哪个模型 |
| `WBBRIDGE_ALLOW_PAID` | 未设 | 设为 `1` 才允许本地 `dev/` 跑计费模型 |

（**别设** `PYTHONUTF8`/`PYTHONIOENCODING`：中文提示会与控制台代码页不匹配而乱码。
脚本已自行按控制台代码页重配输出。）

---

## 卸载

```cmd
uninstall.cmd
```

移除 provider 段、profile、目录文件、技能，以及 `AGENTS.md` 里那段规则（有备份）。
包目录本身不删。

---

## 安全与合规

- **代理无鉴权**（本地不校验密钥），默认只绑 `127.0.0.1`。**不要**改成 `0.0.0.0` 或做
  局域网共享——那等于把账号令牌暴露给整个网络。
- 凭据由 WorkBuddy 桌面端自己的 Electron 提供密钥，本包**不存储也不打印**任何令牌明文。
- 上游是**私有逆向接口**：改一次规则就可能整体失效。绕过 system prompt 指纹（`code 11128`）
  属于**主动规避上游风控**。
- 因此：**技术上可以拷给别人用，但请勿公开分发、打包发布或写教程**。这既是合规问题
  （违反上游 ToS），也是自保问题——一旦被大规模使用，接口被封，所有人的号都不能用了。
  自己人之间小范围传无所谓，别放到公开仓库/网盘/社交平台。
- **让每个使用者自己登录自己的账号**：不要把自己的凭据文件拷给别人（本包也不会为之提供便利）。

**别滥用免费档**：`hy3` / `deepseek-v4.1-flash` / `step-5-preview` 实测 0 扣分，
但高频跑会触发上游账号级限流（`code 14003`，429），影响的是自己的号。当它是个彩蛋，
不要当免费 API 刷。

---

## dev/ 里的诊断脚本（本地，不随仓库发布）

这些脚本**不包含在公开发布的仓库里**（含逆向分析与指纹定位内容），仅你本地排查时用。
**默认全部走免费档**（`deepseek-v4.1-flash`，实测 0 扣分），
换成别的模型用 `WBBRIDGE_MODEL=hy3` 之类。

真正花钱的费率测量脚本（`_measure_*.py`、`_codex_bench.py`、`_compare_codex.py`）
**默认拒绝运行**，必须显式同意，免得拿到包随手一跑就扣积分：

```cmd
set WBBRIDGE_ALLOW_PAID=1
python dev\_measure_rate.py
```

| 脚本 | 用途 |
|---|---|
| `_devcommon.py` | 共用路径常量（`BIN`/`TMP`/`WORK`）+ 费用守卫，不写死机器路径 |
| `_test_proxy.py` / `_test_plugin.py` | 代理端到端；识别上游 429 并自动重试一次 |
| `_probe_429.py` | 直连上游抓 429 的完整响应体（定位到 `code 14003`） |
| `_stub_stream.py` | 桩服务器：重放真实 SSE 事件流，验证流式行为与保活 |
| `_bisect_*.py` | 二分定位上游指纹触发点（`code 11128`） |
| `_measure_rate.py` / `_compare_codex.py` / `_codex_bench.py` | 费率与 Codex 成本对比 ⟵ 计费 |
| `_measure_gpt6.py` | 各 GPT-6 档的积分率 ⟵ 计费 |
| `_ttfb_matrix.py` | 首字节延迟矩阵（默认免费档；`WBBRIDGE_ALLOW_PAID=1` 才加测收费档） |

（以上均**不在**公开发布的仓库中。）
