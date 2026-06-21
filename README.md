# OmniDesk Agent

OmniDesk Agent 是一个“本地优先”的多渠道 AI 助理工程骨架，目标是把三类能力合并到一个安全可控的运行时：

1. **Computer-use**：看屏幕、截图、点击、输入、热键、跨软件操作。
2. **Gateway + channels**：Telegram、WhatsApp Cloud API、WeChat Official Account、Facebook Page / Instagram Graph API、以及用户手动授权的桌面 UI Bridge。
3. **Agent runtime**：层级规划、经验检索、skills/tools 加载、文件/shell/系统 UI 操作、每一步执行前权限验证。
4. **OpenClaw-aligned interaction layer**：参考 OpenClaw 的多渠道产品形态，提供渠道生态目录、交互画像学习和 UI Bridge 目标推荐；安全架构仍坚持 OmniDesk 的审批、审计、签名、沙箱和生产闭环。

> 合规边界：本工程不包含绕过登录、破解平台接口、批量骚扰、隐蔽操控个人账号、规避官方限制的代码。WhatsApp/Meta/WeChat 的正式接入默认使用官方 API；对个人账号只能通过“用户当前已登录、用户可见、每步批准”的 UI Bridge 执行桌面操作。

## 快速开始

```bash
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
apps/
  web-admin-next/              Web Admin 控制台
  desktop-tauri/               桌面 Control Hub / 本地 Runtime
  mobile-flutter/              移动聊天、审批、通知、任务状态
  shared/                      跨端 API contract
omnidesk_agent/
  cli.py                       命令行入口
  daemon.py                    常驻运行时
  server.py                    FastAPI webhook/API 网关
  config.py                    配置模型
  core/                        规划、执行、LLM 抽象
  memory/                      SQLite 经验检索
  security/                    权限验证、审计日志
  tools/                       computer/shell/files 工具
  channels/                    Telegram/WhatsApp/WeChat/Meta/UI Bridge + ecosystem catalog
  learning/                    经验抽取、交互画像、日常学习任务
  skills/                      SKILL.md 加载器
packages/                      agent-runtime / connector-sdk / policy / approval / audit / memory 边界说明
infra/                         docker / k8s / otel 工业入口
release/                       外部 GA evidence contract、模板、审计报告
tests/                         单元、集成、e2e、load 分层入口与现有测试矩阵
examples/config.yaml           配置样例
deploy/systemd                 Linux 后台常驻
deploy/launchd                 macOS 后台常驻
```

## Release Boundary

当前版本是 `1.12.5+root-monorepo-production-ga-candidate`。仓库根目录已经暴露源码、应用、基础设施、测试、工作流和证据 gate；但 customer-distribution Production GA 仍要求真实外部证据：

```bash
python scripts/check_external_ga_evidence.py .
```

该命令必须在无 `--audit-only` 情况下通过后，才能声称正式 GA。

GitHub `main` 必须保持为源码主干；package-only zip 只能作为 Release/Actions artifact 或备份分支保留。详见 [INDUSTRIAL_SOURCE_MAIN_RESTORE.md](INDUSTRIAL_SOURCE_MAIN_RESTORE.md)。

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
- OpenClaw 参考生态：Slack、Discord、Google Chat、Signal、iMessage、Microsoft Teams、Matrix、Mattermost、Zalo、QQ、WebChat、Voice Wake、Live Canvas 等被记录为 `ecosystem_reference`，可用于规划和 UI Bridge 目标推荐，但不会被视为 OmniDesk 原生已支持通道。

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

---

## Self-Learning Loop

Omni-deskAi now includes a safer self-learning loop:

```text
task result
  -> structured experience extraction
  -> failure classification
  -> experience retrieval before planning
  -> daily learning report
  -> growth-oriented improvement proposals
  -> approval gate
  -> sandbox tests
  -> rollback-aware upgrade workflow
```

New commands:

```bash
omnidesk learning-report --days 7
omnidesk metrics --days 7
omnidesk experience-search "browser captcha login" --limit 5
```

The system prefers low-risk skill/workflow improvements before core runtime changes. Core planner, permission, shell, security and self-upgrade changes require human approval.


---

## Governed Self-Improving Agent

Omni-deskAi now includes a governed self-upgrade pipeline:

```text
Upgrade Proposal -> Scoring -> Risk Classification -> Permission Diff -> Sandbox / Regression Tests -> Shadow Mode -> Canary Release -> Human Approval -> Stable Release -> Upgrade Memory
```

New commands:

```bash
omnidesk upgrade-proposals
omnidesk upgrade-artifact <proposal_id>
omnidesk upgrade-feedback <proposal_id> rejected --reason "too risky"
```

Local dashboard:

```text
/self-upgrade/dashboard
```


---

## Admin / Webhook / Planner Hardening

Management APIs now use unified AdminAuth; webhooks pass through WebhookSecurity with real adapter envelopes; session approvals are scoped by source/actor/risk/scope_hash; resume tokens are one-time and only exist while waiting; planner context is reduced by ToolSelector; low-confidence vision clicks become approval requests; core type checking is exposed through `scripts/check_core_pyright.sh`.


---

## Self-Upgrade Governance Writeback

Upgrade proposals now store regression, security, shadow-mode and canary evaluation results directly in proposal metadata. This closes the loop from proposal generation to evidence-based promotion.

```bash
omnidesk upgrade-evaluate <proposal_id>
```

Management endpoint:

```text
POST /self-upgrade/proposals/{proposal_id}/evaluate
```

The endpoint is protected by AdminAuth.


---

## Industrial Production Beta Hardening

This repository now includes an industrialization baseline:

- Public `/health` returns only liveness and version.
- Detailed runtime state is behind `/admin/status`.
- Admin metrics are behind `/admin/metrics`.
- Request IDs, JSON audit events and local Prometheus-style metrics are available.
- Long-term memory redacts common PII/secrets before persistence.
- Channel adapters expose `verify_request()` and `extract_envelope()` contracts.
- Self-upgrade is PR-only and cannot patch `main` directly.
- Upgrade sandbox supports Docker no-network read-only execution.
- CI includes Python 3.10-3.13 matrix, ruff, blocking pyright, pytest coverage, bandit and pip-audit workflows.


---

## L10 Industrial Learning Observability

OmniDesk now includes a self-learning observability layer with audit events, learning quality metrics, industrial SLO evaluation, and an AdminAuth-protected dashboard.

```bash
omnidesk learning-l10-report --days 7
omnidesk learning-l10-report --days 7 --format html > learning_dashboard.html
```

Admin API:

```text
GET /admin/learning/report?days=7
GET /admin/learning/dashboard?days=7
```

Tracked metrics include task success rate, experience reuse rate, bad memory rate, stale memory rate, contradiction rate, permission bypass rate, rollback success rate, test coverage, learning quality score, and industrial readiness score.


---

## Industrial Runtime Hardening

This version adds role-aware AdminAuth, IP allowlist, admin audit, one-time resume token consumption, Docker-capable shell backend, subprocess plugin output limits, self-upgrade state-machine evidence, and runtime memory governance.


---

## 0.7.12-industrial-rc7

This release adds industrial RC7 hardening: governed memory writes, default-off high-risk capability gates, plugin runtime permission validation, rootless Docker sandbox isolation, PR/artifact promotion metadata, SQLite migration/close governance, strict sandbox smoke probes, SHA-pinned GitHub Actions, release SBOM/signing workflow, initialized runtime metrics, and an 80% global CI coverage gate with security/core/tools grouped gates at 90%/85%/85%.


### 0.7.11 RC6 deployment note

Production Docker deployments should not mount `/var/run/docker.sock` into the OmniDesk app container. Use `sandbox.backend: remote_docker` with an isolated rootless Docker/Podman runner. Production configs must pin sandbox images by digest and keep unsigned release artifacts disabled.
