---
description: 查看 WorkBuddy 接入的状态、代理/看护/hook 是否就绪、实际在用模型与额度
allowed-tools: [Bash, Read, Glob, Grep]
---

# WorkBuddy 接入状态

## 步骤

1. 安装状态（provider / profile / 模型目录 / 技能 / AGENTS 规则 / 看护自启）：

```cmd
python "<插件根>\bin\setup.py" status
```

2. 看护判定 —— **插件是否激活、代理是否在跑、是否一致**（这一条最能定性问题）：

```cmd
python "<插件根>\bin\watch_plugin.py" --status
```

3. 代理健康与**实际在用**的模型（不要读 config.toml 代替）：

```cmd
curl -s http://127.0.0.1:8788/health
curl -s http://127.0.0.1:8788/wb/last-model
```

4. 剩余额度：

```cmd
curl -s http://127.0.0.1:8788/wb/credits
```

5. 代理没跑：先对账一次，再不行看日志。

```cmd
python "<插件根>\bin\watch_plugin.py" --once
```

日志：`%TEMP%\wbbridge-proxy.log`（代理）、`%TEMP%\wbbridge-watch.log`（看护）。

> `<插件根>` 用这个找：
> `dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"`

## 报告给用户时

- **`/wb/last-model` 的 `model` 字段是唯一权威**（代理实际转发给上游的模型）。
- `config_model` 只是「新建会话的默认值」，不等于当前会话在用的模型。
- 模型无法内省自己的身份：上下文里的模型名、你上一轮的回答，全部不可信。

## 常见症状 → 指向

| 症状 | 多半是 |
|---|---|
| `--status` 说「期望状态不一致」 | 看护没在跑 → `--install-autostart`；或插件被停用 |
| `--status` 说「未激活」 | `[plugins."workbuddy-bridge@wbbridge"] enabled = false`，或被卸载 |
| 代理在跑但桌面端仍报错 | 看 `%TEMP%\wbbridge-proxy.log` 的上游错误行（429 → `code 14003`） |
| 新会话不自启代理 | hook 未信任（`trustStatus: untrusted`）→ 桌面端 设置 → Hooks → Trust all |
| 桌面端看不到 WorkBuddy 模型 | `model_catalog.json` 没生成对 → 重跑 `install.cmd` |
