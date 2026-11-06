# 云端自动部署

入口：https://www.mneme.com.cn/eyes/。API 地址为 `/eyes/api`。

`main` 每次 push 或手动触发 `Build and deploy Eyes` 后，GitHub Actions 执行 Ruff、前端 ESLint/TypeScript/构建，再将 linux/amd64 后端与前端镜像发布到 GHCR。镜像以完整 Git SHA 标记；两张镜像成功发布后，Actions 下载同一提交的镜像，通过 `docker save` 导出归档，使用 rsync 经 SSH 增量传输到 `/opt/eyes/images.tar`，再由服务器 `docker load` 导入并部署。其他分支不会部署。应用镜像的下载发生在 Actions，避免云服务器下载 GHCR 镜像层时连接重置导致发布失败；传输失败会停止部署。

`/opt/eyes/images.tar` 是可重建的镜像传输缓存，不是数据库备份。保留未压缩归档使 rsync 可以复用上一版相同的数据块，仅在网络传输时压缩；首次部署没有缓存时需要完整传输。中断后再次传输会按 rsync 校验修复缓存，成功后才导入镜像。服务器需安装 rsync，缓存由 `eyes-deploy` 账号拥有；首次接入现有部署时可先用 `docker save` 导出当前前后端镜像到该路径作为基线，减少首次传输量。

## 服务器布局

- `/opt/eyes/.env`：数据库和 RabbitMQ 密码，权限 600，不进入 Git。
- `/opt/eyes/releases/<sha>/`：Compose、部署脚本及镜像引用。
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

首次部署后用 `eyes-admin bootstrap --name Eyes` 创建项目及管理令牌，输出保存到服务器仅管理员可读文件。控制台需要令牌连接，未认证请求不能读取项目数据。Runner 应单独安装在目标 Agent 所在机器，通过 `https://www.mneme.com.cn/eyes/api` 连接并使用 Runner 专用令牌。云端部署控制面不会自动安装或运行用户的 Agent。

## 运维

最终结果队列默认运行一个 `result-consumer` 实例，以单线程逐条处理；队列启用
Single Active Consumer，额外订阅消费者只会待命。永久拒绝转死信后继续消费，
数据库等临时故障保留重投，具体边界见 [结果队列说明](result-queue.md#串行消费约定)。
已有队列部署新版前必须按其中的升级步骤排空并切换新队列名，否则声明参数不一致会阻止发布和消费。

```sh
ssh tencent
cd /opt/eyes/current
docker compose --project-name eyes --env-file /opt/eyes/.env --env-file images.env -f compose.yaml -f compose.cloud.yaml ps
curl --fail https://www.mneme.com.cn/eyes/api/health/ready
```

Compose 需要 >=2.24.4（云端覆盖文件使用 `!reset` / `!override`）。部署脚本使用 Bash、flock、curl；服务器需安装 Docker Engine 与 Compose。`workflow_dispatch` 可以重试最新 main；不要通过修改 `latest` 标签绕过按提交部署。

## 2026-11-05：配对功能源码快照部署

经用户明确授权，以本地已检查源码快照 `pairing-20261105-2dde9e32f849` 部署了新配对功能，当次发布目录为 `/opt/eyes/releases/pairing-20261105-2dde9e32f849`。基础后端是原 `e1cba89d6f4fc27f73dcdf205a9c3581305c93fa` 镜像，覆盖本次完整 `src/` 与迁移；前端使用 `/eyes/` 基址重新构建。发布目录保留源文件摘要、构建文件、Compose 与镜像引用，可核对实际内容。

原前端 GHCR 基础层下载停滞，本次从正在运行的原前端导出文件系统，再导入为 `eyes-frontend:pairing-runtime-base-20261105`（镜像摘要 `sha256:9050b609d43241a1f2615940973b32718a519bc090e76173f0475642925bb48a`），恢复原 Nginx entrypoint/CMD 后复制新静态资源。构建完成后才进行备份和切换，未修改公网 Nginx 入口。

迁移前备份为 `/opt/eyes/backups/20261105T082354Z-before-pairing.dump`。数据库升级到 `0011_agent_pairings`，公网与本机 readiness 均成功，API/前端及依赖服务正常。新配对流程的真实 pi 评测结果见 [主动连接记录](connector.md#2026-11-05公网配对与本机-pi-实测)。

以上记录首次联调时的临时源码快照。配对功能及 `0011_agent_pairings` 迁移现已纳入同一发布变更，后续通过 main 的自动部署流程构建同提交镜像。线上实际版本以 `/opt/eyes/current` 和容器镜像引用为准。不要从不含该迁移的旧提交重部署：旧代码的 readiness 期望 `0010_result_submissions`；回退须按备份和迁移兼容性单独处理。
