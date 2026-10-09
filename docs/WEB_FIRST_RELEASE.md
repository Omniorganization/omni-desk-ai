# 网页首发：独立范围、验收和部署

本范围只交付 Web 和服务器运行源码。Apple/Windows/Android 原生安装包均不分发。现有 Tri-App Quality、Customer Distribution GA 和所有外部 GA 门禁保持原样。

网页首发候选与完整工业 GA 分别标记。`customer_ga=false` 始终保留；通过临时云端浏览器验收不会把 `persistent_deployment` 改成 true。生产可用性必须由真实长期主机另行产生证据。没有主机时不可返回上线 URL。

## 云端候选验收

`Web First Candidate` 运行锁定依赖、Web 类型/单元检查、实际生产镜像构建、私有 HTTPS、真实 PostgreSQL、真实 rootless sandbox，随后使用 Chromium 验证：

- 错误令牌拒绝、真实角色、HttpOnly/Secure/Strict cookies、CSRF 和只读写操作拒绝。
- 浏览器生成不可导出的 P-256 内存密钥、owner 入网挑战、签名审批及 durable nonce 重放拒绝。
- 实际项目创建、刷新后的 PostgreSQL 状态、退出和浏览器 cookie 失效。
- 实际免费 Ollama `smollm2:135m` 问答并持久化，明确标记临时测试模型，不能代表生产质量或持续供应。
- 实际 SSE 工作区恢复已验证 session 并交付答案；非原生 provider 流式明确标记为审计后流式交付。
- 后台进程重启、独立数据库恢复及容器 CPU/内存/PID/只读/权限限制。

设备私钥不再写入 IndexedDB、localStorage 或 sessionStorage。刷新/新会话产生新的设备身份。viewer 无需设备入网即可读取；operator 可注册设备并执行普通项目/聊天；owner 才能发起设备入网和签名审批。审核与设备策略未降低。

AppSync 连接池现在在正常事务退出时提交，异常时仍回滚并丢弃连接。真实网页验收发现项目创建响应成功但连接归还时被回滚；跨实例 CRUD/失败回滚测试与页面刷新检查覆盖此问题。

候选包包含实际测试镜像、运行源码、浏览器证据、后台证据和逐项 SHA256 绑定。`web_first_release.py verify` 拒绝原生分发、不同提交/运行、篡改、符号链接、遗漏行为、运行错误及假生产声明。

## 审核与发布

1. 本变更及依赖 PR 必须经过正常独立 Code Owner 审核，合入 main；不使用自审、管理员绕过或直接 main 写入。
   依赖包括 #133、#134，以及实际 PostgreSQL 问答必需的 #131（调用计数、连接生命周期和规划器内存隔离）。本分支已合入 #131 的源提交进行综合验证；这不代表该 PR 已在 main 获批。
2. 在合入的 main 上运行 `Web First Candidate`。PR 合并候选的证据不能替代 main 的实际包。
3. 在 main 上触发 `Web First Protected Release`，传入该 main 候选运行 ID。既有 release environment 的独立批准仍然生效。
4. 发布流程仅接受成功、同提交、main 的对应候选，产生 GitHub OIDC artifact provenance，避免复制任何发布私钥到服务器。该产物仍是受保护的网页试用候选，不冒充完整 Customer GA。

## 长期服务器条件

仍需实际可用 Linux x86_64 主机、管理员及专用非 root 用户、有可验证的公开 HTTPS 证书、PostgreSQL 16 或匹配的客户端工具、独立备份位置、rootless Podman/cgroup v2（cpu/memory/pids）、Node 镜像可加载和实际 Ollama 内存容量。OS 必须配置 subordinate UID/GID、用户会话 linger 和真实 controller delegation；不能改成 rootful、移除资源控制或公开后台端口。免费资源的账号/存量/回收约束不能由候选包证明。

仅 443 作为应用公网入口；管理 SSH 限制操作员来源。18789、18890、13000 和 PostgreSQL 只通过 loopback/private 访问。系统防火墙和云安全组两层均需读回验证。TLS 续期和异常证书处理必须实测。

## 主机安装顺序

以下步骤在获授权的真实服务器上执行，不在用户的 Mac 运行项目。

1. 下载 `web-first-protected-release`。用 `gh attestation verify` 对 `web-candidate.json`、`web-image.tar.gz`、`runtime-source.tar.gz` 验证仓库 `Omniorganization/omni-desk-ai`、签名工作流 `.github/workflows/web-first-release.yml` 和 main 的来源；再次运行包校验绑定到已审核 main SHA。哈希本身不能代替 provenance。
2. 只解压验证过的运行源码到新的 `/opt/omnidesk-web-first/releases/<sha>/`。在该目录建立 Python 3.11 venv，用 `--require-hashes` 安装 bootstrap/runtime/enterprise 三个 lock，再 `pip install . --no-deps --no-build-isolation`。不覆盖旧槽位。
3. 以专用非 root 用户加载验证过的 Web 镜像；确认加载后的 image ID 等于 manifest 中的 `web_image_id`，预拉取 digest-pinned sandbox image，并确认 rootless/cgroup 实际限制。为所有角色建立真实组织/用户绑定。
4. 通过私密环境变量提供实际 PostgreSQL DSN，在运行源码目录执行 `scripts/web_first_host.py init`，参数为真实 HTTPS origin、专用用户、actor、现存 TLS 证书/私钥路径、已验证 Web image ID。该命令使用 exclusive/0600 创建配置，拒绝覆盖已有密钥，不打印令牌，不启动服务。
5. 对现存数据库先备份，停止旧实例设备写操作；显式执行 AppSync migrate（包含迁移 4），再执行 `--check`。禁止混合旧版本设备写入；保留 nonce rows。
6. 仅在 production environment 独立批准后，将生成的三份 systemd 单元安装到 `/etc/systemd/system/`，设置 `current` 指向该槽位，验证 nginx 配置后启用 HTTPS 反向代理与服务。先私有验收，再开放授权用户。
7. 在该真实地址重复网页角色/入网/审批/重放/项目/模型/刷新/退出/失效验收，验证重启自启动、备份恢复、告警和回滚。确认全部端口的实际可见性。保留生产 origin、时间、source SHA/image ID、结果和 redacted evidence。不要上传 cookie、DSN、私钥、配置或备份内容。

## MONICO 的 Gateway-only 模式（仅准备，未部署）

已有 MONICO 网页时，可在同一审核过的源码槽位执行 `init --gateway-only`，复用原有 PostgreSQL、生产配置、安全验证、备份恢复及两份 Gateway/runner systemd 单元。此模式不需要 `--web-image-id`，不生成 `web.env` 或 Next 服务，也不运行或激活任何服务；省去 Web 镜像不等于省去 PostgreSQL、rootless Podman 沙箱和实际模型。默认 `--web-image-id` 模式仍生成原来的三份单元；两模式不可同时指定，不支持在已有配置上原地切换。

在获授权的 Linux 主机完成上述来源验证、Python 依赖、专用 `omnidesk` 用户、私有数据库、TLS、rootless/cgroup 限制及模型准备后，通过私密环境变量提供 DSN，并填写实际 origin、证书路径与 actor：

```sh
sudo --preserve-env=OMNIDESK_POSTGRES_DSN ./venv/bin/python scripts/web_first_host.py init \
  --gateway-only --origin "$OMNIDESK_PUBLIC_ORIGIN" --user omnidesk --actor "$OMNIDESK_ACTOR" \
  --cert "$OMNIDESK_TLS_CERT" --key "$OMNIDESK_TLS_KEY" --model smollm2:135m
```

生成配置仍只写 `/etc/omnidesk/web-first` 和 `/var/lib/omnidesk-web-first`，拒绝覆盖现有配置/密钥。模型的四份 profile 与 `runtime.required_ollama_models` 使用同一 `--model` 值；模型下载、容量和实际回答仍需主机验证。已有 x86_64 候选验收不能证明 Oracle A1/arm64 兼容，arm64 的锁定依赖、沙箱镜像及 Ollama 需单独验证。

独立的 `nginx.gateway-only.conf.template` 仅将 `/admin/session/identity`、`/api/chat/stream` 及 MONICO 当前使用的项目、会话问答/消息、审批、通知、设备注册/入网和单任务读取路径反代至 `127.0.0.1:18789`，其他路径返回404。它不开放通配的 `/app/`、管理状态、WebSocket、desktop claim、push dispatch 或任务执行接口。请求方法、RBAC、组织权限、设备签名、幂等和审批仍由后端校验；无 URI 重写以保留签名路径，SSE 关闭响应缓冲和缓存。仅443可公开，runner18890、Ollama11434、PostgreSQL及Gateway18789均保持私有。

Gateway-only 模板明确清空 `X-Forwarded-For`，避免 Uvicorn 信任 loopback 代理后用公网地址替换 socket peer、使原 `admin_allowed_ips=[127.0.0.1]` 拒绝身份请求。令牌仍必需，IP白名单和本地免令牌禁用保持原样。实际 peer 留在 nginx access log；后端按代理共享 IP 限额，另有 actor/role/org 配额。不可扩大 IP 白名单或直接信任用户传入的代理头。

激活前仍需数据库备份/迁移4检查、独立批准、安装仅两份生成的单元、设置审核槽位 `current`、实际 `nginx -t` 及防火墙/云安全组读回。通过真实 HTTPS 端到端身份、错误令牌、角色、设备入网/签名重放、持久化、SSE、重启和恢复验证后，才可配置 MONICO 的实际 Gateway 地址。当前本地测试只证明配置生成、有限路由和代理鉴权边界，未创建服务器、DNS、TLS或凭据，未证明模型/任务执行、长期可用性或生产上线。

风险包括公网有限 API 入口、共享 IP 限额、模型质量/容量、TLS续期和单机故障；首次激活前保留私密备份并准备停服回退。回滚须停止写入与新入口，切回已验证上一源码/配置槽位，保留迁移4及 nonce 数据并复验；首次部署无旧槽位时停用入口/服务并保留受限状态和密钥，不自动删除或重新生成身份。

本地准备验证（2026-10-07，Python 3.11.15）：以下针对性检查72项通过，覆盖两模式实际配置生成、防覆盖、有限路由、真实 Uvicorn 代理头处理、生产策略和恢复保护。配置生成测试全部写入临时目录并替换凭据/TLS/主机操作。原脚本在新增的前7项检查中5项失败、2项通过；修复后包含代理头回归的8项均通过。当前机器无 nginx，未执行实际 `nginx -t` 或主机部署验收。

```sh
/private/tmp/omnidesk-ios-20261007/checks-venv311/bin/python -m pytest tests/test_web_first_host.py tests/test_web_first_release.py tests/test_production_config_validator.py tests/test_admin_auth_production_default.py tests/test_api_resource_guard.py -q --tb=short
```

## 备份、恢复和回滚

`web_first_host.py backup` 使用私密 DSN 和匹配版本的 pg_dump，保存 custom backup 和 SHA256。备份目录必须是 0700，文件离机备份必须受限并加密；同时备份实际应用状态、审计及密钥至另一个私密位置。数据库单份快照不等于完整灾难恢复。

`web_first_host.py restore` 只接受另一个名称且为空的数据库，先验证备份哈希，拒绝覆盖生产数据库。数据库恢复后应在隔离配置上检查 migration 4、消息和旧 nonce 拒绝，再决定切换。pg_restore 不使用 `--clean`。

回滚前停用写入，切回已验证的上一槽位及 Web image ID，重启并复验。保留 schema migration 4 和 nonce 数据；降级到不支持 durable nonce 的旧后台会恢复已知重放风险，必须独立决定，不能宣称安全回滚通过。首次部署没有可回滚的旧版本，需要先保留候选/数据库/密钥和停服回退方案。

## 当前边界

免费托管网页不能替代长期 Python/PostgreSQL/rootless 沙箱。零预算且没有已激活主机时，云端验收和可审查发布包可以完成，长期上线仍受真实资源限制。Apple/Windows 原生发布延期，不参与本范围；完整工业 Customer GA 的生产/HA/soak/真实集成条件仍由原门禁判定。
