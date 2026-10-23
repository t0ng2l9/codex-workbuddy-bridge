---
name: workbuddy-bridge
description: >-
  把 WorkBuddy AI 桌面端已登录的模型接进 Codex（CLI 与桌面端），或排查该接入的问题。
  Use when the user wants to use WorkBuddy/CodeBuddy models in Codex, asks about the
  workbuddy model provider, the local proxy on 127.0.0.1:8788, the free WorkBuddy models
  (hy3 / deepseek-v4.1-flash / step-5-preview), the /wbbridge-setup or /wbbridge-status
  commands, hook trust, the proxy watcher/autostart, model-switching not taking effect,
  or 429 rate limits from the WorkBuddy upstream.
metadata:
  short-description: WorkBuddy 模型接入 Codex
---

# Codex × WorkBuddy Bridge

让 Codex 用 WorkBuddy AI 桌面端的登录态当模型源 —— 不需要 OpenAI 订阅，也不需要 Codex 套餐。
Codex 只出它的 agent 骨架（读写文件、跑命令、apply_patch、多轮工具循环），模型换成 WorkBuddy 的。

```
Codex ──► 127.0.0.1:8788（本地代理）──► workbuddy.ai ──► 真实模型
           │  解密桌面端登录态
           │  Responses ⇄ Chat Completions 转换
           │  记录/同步「实际在用」的模型
           │  生成模型切换列表
```

## 插件装在哪 / 怎么找到它

插件被 Codex **复制**到 cache，源码改动不会自动生效：

```
%USERPROFILE%\.codex\plugins\cache\<marketplace>\<plugin>\<version>\
```

下面所有 `<插件>` 都要按真实位置拼。先定位：

```cmd
dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"
```

安装 / 更新（`marketplace add` 只需一次）：

```cmd
codex plugin marketplace add zetongliu219-alt/workbuddy-bridge   :: 或本地目录 / zip 解压处
codex plugin add workbuddy-bridge@wbbridge
python "<插件>\bin\setup.py" doctor
python "<插件>\bin\setup.py" install
```

**Codex 按版本号缓存** —— 改了插件内容要先升版本再重装，否则不生效：

```cmd
python "<插件>\bin\bump-plugin.py" "<插件目录>"
codex plugin add workbuddy-bridge@wbbridge
```

⚠ 重装前确保**没有进程把插件目录当工作目录**（自己 `cd` 进去过、或代理/看护还在跑），
否则报 `failed to back up plugin cache entry: 拒绝访问 (os error 5)`。
先把代理和看护停掉再重装。

## 文件一览

| 路径 | 用途 |
|---|---|
| `install.cmd` / `uninstall.cmd` / `status.cmd` / `doctor.cmd` | 装 / 卸 / 查 / 体检 |
| `bin\wbproxy.py` | 本地代理本体（凭据解密、请求改写、SSE 回译、模型同步） |
| `bin\wbresponses.py` | Responses API ⇄ Chat Completions 适配层 |
| `bin\wbdiscover.py` | 路径自动发现（桌面端 / Python / Codex / 凭据） |
| `bin\gen-catalog.py` | 生成模型切换列表 `wbbridge-catalog.json` |
| `bin\setup.py` | 安装/卸载器（provider、profile、目录、技能、AGENTS 规则、看护自启） |
| `bin\hook_session_start.py` | SessionStart hook 的实现 |
| `bin\wbspawn.py` | 无控制台窗口启动后台进程（venv 的 python.exe 是 uv 存根，转发不传 flags） |
| `bin\watch_plugin.py` | **看护**：按插件启用状态开关代理 |
| `bin\bump-plugin.py` | 更新插件时打 cachebuster 版本号 |
| `scripts\codex-wbb.cmd` | 启动 Codex（代理没跑会自动拉起） |
| `scripts\start-wbbridge.cmd` | 只启动代理（`--verbose` 看请求流水） |
| `scripts\watch-bridge.cmd` | 看护：`--status` / `--once` / `--install-autostart` / `--remove-autostart` |
| `skills\` | 本技能 + `wbbridge-current-model` |
| `commands\` | `/wbbridge-setup`、`/wbbridge-status`（**注意**：本版本 Codex 的
`plugin/read` 不枚举 commands，这两个可能不被暴露；功能等同的命令行见下） |

## 常用操作（可靠路径）

插件命令（`/wbbridge-setup` 等）**在部分 Codex 版本里不会被暴露**，所以日常直接用脚本：

```cmd
python "<插件>\bin\setup.py" doctor        :: 体检
python "<插件>\bin\setup.py" install       :: 安装（幂等）
python "<插件>\bin\setup.py" status        :: 安装状态
python "<插件>\bin\watch_plugin.py" --status   :: 看护判定
```

## 代理的生命周期：两层保障

**代理不需要手动启动。** 两层互补：

1. **hook（会话级）** —— `SessionStart`：新开会话时确保代理在跑。
2. **看护（常驻）** —— `install.cmd` 装成**登录自启**，每 5 秒对账：
   **插件激活 → 代理在跑；插件停用/卸载 → 停掉代理**（实测 4 秒内跟随）。

| 场景 | 谁负责 |
|---|---|
| 新开 CLI 会话 / 桌面端新聊天 | hook |
| 机器重启后 | 看护 |
| 已开着的会话里代理中途挂了 | 看护（几秒自愈） |
| 在 Codex 里停用插件 | 看护（自动停代理） |
| 不用 Codex 也想常驻 | 看护 |

### hook 必须被信任一次，否则永远不执行

未信任时它的状态是 **`untrusted`**，**不会跑** —— 这正是「代理没起来、桌面端静默失效」
最常见的原因。查状态（起 app-server 问 `hooks/list`）会看到：

```
key:         <plugin>@<market>:hooks/hooks.json:session_start:0:0
enabled:     True
trustStatus: untrusted        ← 就是这个
```

**桌面端**：设置 → **Hooks** → 来源分组 **From Plugins** 下找到本插件 `SessionStart`
（未信任时显示 *Disabled until hook is trusted*）→ 点 **Trust all**。

**TUI**：新开会话时弹 *Hooks need review … [Trust all and continue]*，选它。

**命令行 / 自动化**：`codex exec --dangerously-bypass-hook-trust ...`

信任状态存在 `config.toml` 的 `hooks.state.<key>.trusted_hash`；**改过 hook 内容后要重新信任**
（会显示 *Hook changed since last trusted*）。若提示 *Another config layer overrides these hook
settings*，是有更高优先级的配置层压着。

### 看护命令与日志

```cmd
python "<插件>\bin\watch_plugin.py" --status
```

| 参数 | 作用 |
|---|---|
| （无） | 常驻看护 |
| `--status` | 打印判定：插件是否激活、代理是否在跑、是否一致 |
| `--once` | 只对账一次 |
| `--install-autostart` | 装登录自启（写 VBS 到启动文件夹，免管理员） |
| `--remove-autostart` | 取消登录自启 |

日志：`%TEMP%\wbbridge-watch.log`（只记状态变化与错误）。自启文件：
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\workbuddy-bridge-watch.vbs`。

看护**不会误杀别人的程序**：停代理前核对进程命令行含本包 `wbproxy.py`；
端口被别的程序占用时报 `foreign-port` 并什么都不做。

## 用法

```cmd
codex --profile wbb                        新会话走 WorkBuddy
<插件>\scripts\codex-wbb.cmd               等价，且代理没跑会自动拉起
<插件>\scripts\codex-wbb.cmd -m hy3        换模型
```

桌面端（ChatGPT.exe / Codex app）同理，但**必须新建会话** —— provider 与模型是**逐会话绑定**的，
旧会话记着自己创建时的设置，改配置不会让它跟随。

## 免费档（实测 0 积分）

`hy3`、`deepseek-v4.1-flash`、`step-5-preview`。

⚠ `hy4-preview` **名字带 preview 但是收费的**（实测 0.02）——别凭名字判断。
默认档取 `deepseek-v4.1-flash`（免费里上下文 300k / 输出 128k 最大）。

## 「当前在用哪个模型」必须现查

模型**无法内省自己的身份**，而 `config.toml` 里的 `model` 只是**新建会话的默认值** ——
在会话里用选择器切换是**运行时覆盖，不会写回配置**。所以不要读配置回答，要问代理：

```cmd
curl -s http://127.0.0.1:8788/wb/last-model
```

代理是唯一看到真实 `model` 字段的一方。`/health` 也带 `last_model`。
细节见同插件的 `wbbridge-current-model` 技能。

## 上游 429（`exceeded retry limit, last status: 429`）

**上游账号级限流**，不是代理的 bug。响应体 `code 14003`，官方提示
「等待约 1 分钟，或切换到负载较低的模型」。429 不计费。
实测连续 8 次请求就出现 `200,429,200,429…` 交替 —— 按账号整体活跃度算，不看你打得多快。
代理日志里默认就有一行可读摘要（不需要 `--verbose`）：

```
[wbproxy] 上游错误 429: code=14003 / too many requests / 建议等待约 1 分钟后重试，或切换到负载较低的模型。
```

## 排错顺序

1. **代理在不在**：`curl -s http://127.0.0.1:8788/health`
2. 不在 → 先看**看护**（`watch_plugin.py --status`）；没装就 `--install-autostart`，或先 `--once` 应急
3. 看护说「未激活」→ 在 Codex 里启用插件（`config.toml` 的 `[plugins."…"] enabled`）
4. 起不来 → `doctor.cmd`，看是缺桌面端、缺凭据、还是缺 cryptography
5. 起来了但报错 → 看日志 `%TEMP%\wbbridge-proxy.log`
6. 报 429 → 等 1 分钟或换模型
7. 桌面端看不到 WorkBuddy 模型 → 模型目录 `model_catalog.json` 没生成对，重跑 `install.cmd`
8. 桌面端旧会话不跟随 → **新建会话**
9. 想让「新会话自动起代理」也生效 → 按上文**信任 hook**

## 合规

上游是**私有逆向接口**，绕过其 system prompt 指纹属于主动规避风控。
**自己人小范围用没问题，不要公开分发、打包发布或写教程。**
让每个使用者登录自己的账号，不要传别人的凭据文件。
