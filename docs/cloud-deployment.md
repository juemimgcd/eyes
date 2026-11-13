# 云端自动部署

入口：https://www.mneme.com.cn/eyes/。API 地址为 `/eyes/api`。

`main` 每次 push 或手动触发 `Build and deploy Eyes` 后，GitHub Actions 执行 Ruff、前端 ESLint/TypeScript/构建，再将 linux/amd64 后端、前端与 Gateway 镜像发布到 GHCR（Gateway 构建包含 Go vet 和三个平台客户端打包）。镜像以完整 Git SHA 标记；控制端、前端和 Gateway 三张镜像成功发布后，Actions 下载同一提交的镜像，通过 `docker save` 导出并解开归档，使用 rsync 经 SSH 按文件校验同步到 `/opt/eyes/image-cache/`，再由服务器重新打包并通过 `docker load` 导入部署。其他分支不会部署。应用镜像的下载发生在 Actions，避免云服务器下载 GHCR 镜像层时连接重置导致发布失败；传输失败会停止部署。

`/opt/eyes/image-cache/` 是可重建的镜像传输缓存，不是数据库备份。rsync 按文件校验内容，复用相同的镜像层，仅传输新增或变化的文件；首次部署没有缓存时需要完整传输。同步结束时仅在此专用缓存目录删除旧版本不再引用的文件，不删除 Docker 镜像或持久化数据卷。服务器需安装 rsync，缓存由 `eyes-deploy` 账号拥有；首次接入现有部署时可先用 `docker save` 导出当前应用镜像，并解开到缓存目录作为基线，减少首次传输量。

## 镜像体积与慢链路处理

- 后端先按 `pyproject.toml`、`uv.lock` 安装第三方依赖，再复制 README 和源码并安装 Eyes。仅修改源码或 README 不会使依赖安装层失效；uv 可执行文件仅在构建时临时挂载，下载缓存使用 BuildKit cache mount，两者均不进入运行镜像。容器内直接使用 `eyes-*`、`alembic`、`python` 等已安装命令，不使用 `uv run`。
- Gateway 与三个平台的连接程序去除调试符号，Gateway 程序与客户端下载目录分别保存为镜像层。只修改 Gateway 时，可复用未变化的客户端下载层。
- Actions 将准备镜像、传输、导入发布拆成独立步骤；`docker save` 通过管道直接解开到按层缓存目录，不在 Actions 临时目录额外保存完整 tar。传输步骤输出进度和统计，便于区分构建慢、下载慢及 SSH 传输慢。镜像层已压缩，不再对传输重复启用压缩。
- 传输中断时，rsync 将未完成文件保存在缓存内的 `.rsync-partial/` 目录。重跑同一提交可使用已完成层及保留的部分文件继续传输，完成后才导入镜像；打包导入时排除断点目录。该目录仅供部署账号使用，不是可直接部署的镜像，也不是业务数据。
- 以上优化减少传输量和中断后的重复工作，不保证提高 GitHub 与服务器之间的公网带宽。若新增大层仍长期低速，应接入经过验证的国内仓库或传输节点；仅重试 `docker pull`、更换 DNS 或增加超时时间不构成链路修复。

## 服务器布局

- `/opt/eyes/.env`：数据库和 RabbitMQ 密码，权限 600，不进入 Git。
- `/opt/eyes/releases/<sha>/`：Compose、部署脚本及镜像引用。
- `/opt/eyes/tunnel/runner.env`：现有项目 Runner 的 `EYES_RUNNER_TOKEN`，权限 600。
- `/opt/eyes/tunnel/runner.toml`：项目 Runner 配置，使用内部 Gateway 地址。该目录由部署账号 `eyes-deploy` 拥有，权限 700。
- `/opt/eyes/current`：最近一次通过本机健康检查的部署目录。
- `/opt/eyes/backups/`：已有数据库在迁移前生成的 pg_dump 备份。
- Docker 项目名 `eyes`，PostgreSQL、RabbitMQ、产物使用持久化卷。
- 只有前端映射到 `127.0.0.1:8180`，数据库、消息队列和 API 无宿主机公开端口。
- 现有 HTTPS Nginx 引入 `deploy/nginx-eyes.conf`；转发时剥离 `/eyes/` 前缀。前端构建基址和 BrowserRouter 为 `/eyes/`，FastAPI root_path 为 `/eyes/api`。

部署账号 `eyes-deploy` 属于 Docker 组，属于高权限运维账号。仓库 Secrets `EYES_DEPLOY_SSH_KEY` 和 `EYES_DEPLOY_KNOWN_HOSTS` 分别存放专用私钥和已核对的主机公钥。GHCR 认证仅在 Actions 内使用短期 GITHUB_TOKEN，不再将仓库令牌发送到服务器。不要在日志或 Git 中保存密钥。

## 部署顺序与失败处理

应用镜像导入完成后，部署使用文件锁串行执行：仅拉取本机缺失的镜像 → 启动依赖 → 备份已有数据库 → Alembic upgrade head → 更新 API/后台进程/前端 → 本机 ready 检查 → 更新 current → 公网 ready 检查。PostgreSQL、Redis、RabbitMQ 缺失或更换版本时仍需服务器访问其镜像仓库。迁移失败会停止流程；更新后的健康检查失败会令 Actions 失败，不会自动回滚数据库。Compose 更新有短暂中断，不保证零停机。

此前成功部署目录和数据库备份保留。需要回退时先确认数据库迁移向后兼容，再在对应 release 目录运行 `bash release.sh`；不兼容迁移须人工恢复数据库及一致的产物卷。自动备份只覆盖 PostgreSQL，完整灾备还须保存 artifacts 卷；本流程不会自动删除备份或数据卷。

## 首次使用

生产发布固定合并 `compose.yaml`、`compose.cloud.yaml`、`compose.tunnel.yaml`、`compose.tunnel.cloud.yaml`，并启动 Gateway 与项目 Runner。发布前须按 [隧道部署](tunnel.md#云端部署) 为项目准备 `/opt/eyes/tunnel/runner.env` 和 `runner.toml`；发布脚本不会生成或替换凭据。每次发布使用同一 Git SHA 的三张镜像，隧道不会在自动更新时丢失。持久配置文件缺失时 Compose 配置检查会在迁移及服务切换前失败。

首次部署后用 `eyes-admin bootstrap --name Eyes` 创建项目及管理令牌，输出保存到服务器仅管理员可读文件。控制台需要令牌连接，未认证请求不能读取项目数据。Runner 应单独安装在目标 Agent 所在机器，通过 `https://www.mneme.com.cn/eyes/api` 连接并使用 Runner 专用令牌。云端部署控制面不会自动安装或运行用户的 Agent。

## 运维

最终结果队列默认运行一个 `result-consumer` 实例，以单线程逐条处理；队列启用
Single Active Consumer，额外订阅消费者只会待命。永久拒绝转死信后继续消费，
数据库等临时故障保留重投，具体边界见 [结果队列说明](result-queue.md#串行消费约定)。
已有队列部署新版前必须按其中的升级步骤排空并切换新队列名，否则声明参数不一致会阻止发布和消费。

```sh
ssh tencent
cd /opt/eyes/current
docker compose --project-name eyes --env-file /opt/eyes/.env --env-file images.env -f compose.yaml -f compose.cloud.yaml -f compose.tunnel.yaml -f compose.tunnel.cloud.yaml ps
curl --fail https://www.mneme.com.cn/eyes/api/health/ready
```

Compose 需要 >=2.24.4（云端覆盖文件使用 `!reset` / `!override`）。部署脚本使用 Bash、flock、curl；服务器需安装 Docker Engine 与 Compose。`workflow_dispatch` 可以重试最新 main；不要通过修改 `latest` 标签绕过按提交部署。

## 2026-11-05：配对功能源码快照部署

经用户明确授权，以本地已检查源码快照 `pairing-20261105-2dde9e32f849` 部署了新配对功能，当次发布目录为 `/opt/eyes/releases/pairing-20261105-2dde9e32f849`。基础后端是原 `e1cba89d6f4fc27f73dcdf205a9c3581305c93fa` 镜像，覆盖本次完整 `src/` 与迁移；前端使用 `/eyes/` 基址重新构建。发布目录保留源文件摘要、构建文件、Compose 与镜像引用，可核对实际内容。

原前端 GHCR 基础层下载停滞，本次从正在运行的原前端导出文件系统，再导入为 `eyes-frontend:pairing-runtime-base-20261105`（镜像摘要 `sha256:9050b609d43241a1f2615940973b32718a519bc090e76173f0475642925bb48a`），恢复原 Nginx entrypoint/CMD 后复制新静态资源。构建完成后才进行备份和切换，未修改公网 Nginx 入口。

迁移前备份为 `/opt/eyes/backups/20261105T082354Z-before-pairing.dump`。数据库升级到 `0011_agent_pairings`，公网与本机 readiness 均成功，API/前端及依赖服务正常。新配对流程的真实 pi 评测结果见 [主动连接记录](connector.md#2026-11-05公网配对与本机-pi-实测)。

以上记录首次联调时的临时源码快照。配对功能及 `0011_agent_pairings` 迁移现已纳入同一发布变更，后续通过 main 的自动部署流程构建同提交镜像。线上实际版本以 `/opt/eyes/current` 和容器镜像引用为准。不要从不含该迁移的旧提交重部署：旧代码的 readiness 期望 `0010_result_submissions`；回退须按备份和迁移兼容性单独处理。


## 统一连接版本升级

`0014_connection_purpose` 为既有连接保留 `evaluate` 用途，新建仅观测连接不发布目标，调用授权会拒绝其评测请求。降级迁移会撤销仅观测连接。不得直接用不含该迁移的旧镜像覆盖当前 API；数据库备份和发布目录仍按上述流程保留。

2026-11-08 线上 `unified-connect-20261108` 为源码快照。对应改动进入 main 后，自动发布会将该快照替换为正式 SHA 镜像，并继续使用已有 Runner 凭据和状态卷。此前文档中“后续自动发布可能关闭隧道”的记录描述旧流程，本节所述 Gateway 构建与四文件 Compose 发布已补齐该缺口。云端 Nginx WebSocket 入口由管理员一次性配置，Actions 不修改宿主机 Nginx。
