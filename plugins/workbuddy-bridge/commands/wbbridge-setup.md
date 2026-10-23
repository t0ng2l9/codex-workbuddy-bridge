---
description: 把 WorkBuddy 接入 Codex（安装 / 修复 / 重装）
argument-hint: "[--skip-doctor] [--no-watch] [--no-skill] [--no-agents-rule]"
allowed-tools: [Bash, Read, Glob, Grep]
---

# 安装 WorkBuddy → Codex 接入

用户调用了本命令，参数：`$ARGUMENTS`

## 步骤

1. 先做体检，把结果如实报给用户：

```cmd
python "<插件根>\bin\setup.py" doctor
```

2. 若体检标了 `!!`，按提示补齐后再继续（缺 Python/cryptography、未登录 WorkBuddy 桌面端、
   缺 Codex 都属要先解决的项）。**不要**自己下载或安装 WorkBuddy 桌面端 —— 让用户自己装并登录。

3. 体检通过后安装：

```cmd
python "<插件根>\bin\setup.py" install $ARGUMENTS
```

安装会一并做这些（幂等）：provider 段、profile、模型目录、技能、AGENTS 规则，
以及**代理看护的登录自启**（想让代理按插件启停自动开关，就必须这一步）。

4. 跑 status 与看护判定确认：

```cmd
python "<插件根>\bin\setup.py" status
python "<插件根>\bin\watch_plugin.py" --status
```

5. 告诉用户两件事：

   - 怎么开始用：`codex --profile wbb`，或者桌面端**新建会话**（旧会话逐会话绑定 provider，不跟随）。
   - **建议信任 hook**，否则「新会话自动拉起代理」不生效：
     桌面端 **设置 → Hooks → From Plugins → 本插件 SessionStart → Trust all**
     （或 TUI 新会话时选 *Trust all and continue*；自动化用
     `codex exec --dangerously-bypass-hook-trust`）。
     不信任也能用 —— 看护会兜底。

## 注意

- `<插件根>` 是插件被安装后的真实目录，用这个命令找到它：
  `dir /b /s "%USERPROFILE%\.codex\plugins\cache\wbbridge\workbuddy-bridge"`
- 安装是幂等的：重复运行不会改动已就绪的内容，也不会累积空行。
- 不要修改 `config.toml` 里本插件以外的段落。
- 如果安装失败（退出码非 0），把 `!!` 那行原样贴给用户，不要自己猜。
- 改了插件内容要先升版本再重装（Codex 按版本号缓存）：
  `python "<插件根>\bin\bump-plugin.py" "<插件目录>"` 然后
  `codex plugin add workbuddy-bridge@wbbridge`。
