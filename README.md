# WorkBuddy Bridge — Codex 插件市场

把 **WorkBuddy AI 桌面端（国际版）**已登录的模型接进 **Codex**（CLI 与桌面端）。
不需要 OpenAI 订阅，也不需要 Codex 套餐 —— Codex 只出它的 agent 骨架，模型换成 WorkBuddy 的。

```
Codex ──► 127.0.0.1:8788（本机代理）──► workbuddy.ai ──► 真实模型
           │  解密桌面端登录态
           │  Responses ⇄ Chat Completions 转换
           │  记录/同步「实际在用」的模型
           │  生成模型切换列表
```

插件名 `workbuddy-bridge`，市场名 `wbbridge`，provider `wbbridge`，profile `wbb`，
代理端口 `8788` —— 与机器上其它同类接入**互不冲突**，可以并存。

---

## 安装

```cmd
:: 1. 加市场（GitHub / 本地目录 / 解压出来的 zip 都行）
codex plugin marketplace add <owner>/workbuddy-bridge   :: 或本地目录 / 解压出的 zip 目录

:: 2. 装插件
codex plugin add workbuddy-bridge@wbbridge

:: 3. 体检 + 安装（写 provider / profile / 模型目录 / 技能 / 指令规则）
python "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge\<版本>\bin\setup.py" doctor
python "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge\<版本>\bin\setup.py" install
```

装完 `codex --profile wbb` 即可，或直接 `scripts\codex-wbb.cmd`（代理没跑会自动拉起）。
桌面端用的话：**新建会话**才会走新 provider。

详细说明（排错、模型清单、免费档、hook 信任、代理生命周期）见
[`plugins/workbuddy-bridge/README.md`](plugins/workbuddy-bridge/README.md)。

---

## 收件人需要什么

| 项 | 要求 |
|---|---|
| Windows | 必需（脚本与凭据路径都是 Windows 的） |
| WorkBuddy AI 桌面端（国际版） | 装过并**登录一次**；不需要常驻 |
| Python | 3.10+ 且带 `cryptography`；没装系统 Python 时可用 Codex 自带运行时 |
| Codex | 0.153+（本版在 0.163 上实测通过） |

`doctor` 会逐项告诉你缺什么、怎么补。**让每个使用者登录自己的账号**，不要传凭据文件。

---

## 仓库结构

```
.
├─ .agents/plugins/marketplace.json        市场索引（Codex 据此发现插件）
├─ .gitattributes                          *.cmd 标记 binary（保护 GBK+CRLF 脚本）
└─ plugins/workbuddy-bridge/               插件本体
   ├─ .codex-plugin/plugin.json            插件清单
   ├─ bin/                                 代理与安装器（Python）
   ├─ scripts/                             启动器与 hook 入口（.cmd）
   ├─ skills/                              用法排错 + 当前模型查询
   ├─ commands/                            /wbbridge-setup、/wbbridge-status
   ├─ hooks/hooks.json                     SessionStart：代理没跑就拉起
   └─ README.md                            插件详细文档
```

---

## 分享给别人

三种都实测可用，只差第一步的参数：

| 给法 | 对方怎么装 |
|---|---|
| GitHub 仓库 | `codex plugin marketplace add <owner>/workbuddy-bridge   :: 或本地目录 / 解压出的 zip 目录` |
| 共享目录 / U 盘 | `codex plugin marketplace add D:\share\workbuddy-bridge` |
| zip | 解压后 `codex plugin marketplace add %USERPROFILE%\Downloads\workbuddy-bridge` |

打包 zip 时**必须带上 `.gitattributes`**：它把 `*.cmd` 标为 binary，防止 git 的换行规范化
把 GBK+CRLF 的中文脚本改坏。

---

## 合规

上游是**私有逆向接口**，绕过其 system-prompt 指纹属于主动规避风控。因此：

- **小范围分享可以，别公开分发/写教程**。大规模使用会触发上游风控，接口一封所有人都用不了。
- **别滥用免费档**（`hy3` / `deepseek-v4.1-flash` / `step-5-preview`）：高频会触发账号级限流。
- 代理只绑 `127.0.0.1`，**不要**改成 `0.0.0.0` —— 那等于把账号令牌暴露给整个局域网。

详见 [插件 README 的「安全与合规」](plugins/workbuddy-bridge/README.md#安全与合规)。
