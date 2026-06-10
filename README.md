# OmniDesk Agent

OmniDesk Agent 是一个“本地优先”的多渠道 AI 助理工程骨架，目标是把三类能力合并到一个安全可控的运行时：

1. **Computer-use**：看屏幕、截图、点击、输入、热键、跨软件操作。
2. **Gateway + channels**：Telegram、WhatsApp Cloud API、WeChat Official Account、Facebook Page / Instagram Graph API、以及用户手动授权的桌面 UI Bridge。
3. **Agent runtime**：层级规划、经验检索、skills/tools 加载、文件/shell/系统 UI 操作、每一步执行前权限验证。

> 合规边界：本工程不包含绕过登录、破解平台接口、批量骚扰、隐蔽操控个人账号、规避官方限制的代码。WhatsApp/Meta/WeChat 的正式接入默认使用官方 API；对个人账号只能通过“用户当前已登录、用户可见、每步批准”的 UI Bridge 执行桌面操作。

## 快速开始

```bash
cd omnidesk_agent
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp examples/config.yaml ~/.omnidesk/config.yaml
omnidesk doctor --config ~/.omnidesk/config.yaml
omnidesk serve --config ~/.omnidesk/config.yaml
```

## 常用命令

```bash
# 前台启动网关 / webhook / 本地 API
omnidesk serve --config examples/config.yaml

# 执行一条本地任务；每一步会先走权限验证
omnidesk run "打开浏览器，检查屏幕上是否有未回复的消息"

# 添加经验记录
omnidesk remember "回复客户报价前先检查库存表" --tags sales,inventory

# 检索经验
omnidesk search "报价 库存"
```

## 核心目录

```text
omnidesk_agent/
  cli.py                       命令行入口
  daemon.py                    常驻运行时
  server.py                    FastAPI webhook/API 网关
  config.py                    配置模型
  core/                        规划、执行、LLM 抽象
  memory/                      SQLite 经验检索
  security/                    权限验证、审计日志
  tools/                       computer/shell/files 工具
  channels/                    Telegram/WhatsApp/WeChat/Meta/UI Bridge
  skills/                      SKILL.md 加载器
examples/config.yaml           配置样例
deploy/systemd                 Linux 后台常驻
deploy/launchd                 macOS 后台常驻
```

## 权限模型

每一个副作用动作都会生成 `ActionProposal`，内容包括：工具名、动作名、风险等级、目标、参数、原因、来源渠道。`PermissionManager.verify()` 会根据配置决定：

- `allow`：低风险或白名单动作自动通过；
- `ask`：高风险动作必须用户批准；
- `deny`：危险、越权、未授权来源直接拒绝；
- `dry_run`：只记录计划，不执行。

默认策略是：**看屏幕需要批准，点击/输入/发消息/shell/写文件都需要批准**。你可以在 `examples/config.yaml` 调整。

## Skills

把 OpenClaw/Codex 风格的技能放到：

```text
~/.omnidesk/skills/<skill-name>/SKILL.md
```

启动时会自动加载，交给规划器使用。插件工具可通过 Python entrypoint 或本项目 `ToolRegistry` 注册。

## 渠道接入说明

- Telegram：Bot API webhook 或 polling。
- WhatsApp：WhatsApp Business Platform Cloud API。
- WeChat：微信公众号 / 服务号消息服务器。
- Facebook / Instagram：Meta Graph API，适合 Page、Professional/Business 相关能力；个人账号私信自动化不在官方稳定 API 范围内，使用 UI Bridge 时必须每步用户可见并确认。
- UI Bridge：对 WhatsApp Web、Telegram Desktop、WeChat Desktop、浏览器中的 Meta 页面等已登录软件做屏幕级操作，不保存密码、不绕过 MFA、不隐藏行为。

## 后台常驻

Linux systemd：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/omnidesk-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now omnidesk-agent
```

macOS launchd：

```bash
cp deploy/launchd/com.omnidesk.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.omnidesk.agent.plist
```
