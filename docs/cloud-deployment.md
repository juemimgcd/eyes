# 云端自动部署

入口：https://www.mneme.com.cn/eyes/。API 地址为 `/eyes/api`。

`main` 每次 push 或手动触发 `Build and deploy Eyes` 后，GitHub Actions 执行 Ruff、前端 ESLint/TypeScript/构建，再将 linux/amd64 后端与前端镜像发布到 GHCR。镜像以完整 Git SHA 标记；两张镜像成功发布后，SSH 通知服务器拉取同一提交的镜像并部署。其他分支不会部署。

## 服务器布局

- `/opt/eyes/.env`：数据库和 RabbitMQ 密码，权限 600，不进入 Git。
- `/opt/eyes/releases/<sha>/`：Compose、部署脚本及镜像引用。
- `/opt/eyes/current`：最近一次通过本机健康检查的部署目录。
- `/opt/eyes/backups/`：已有数据库在迁移前生成的 pg_dump 备份。
- Docker 项目名 `eyes`，PostgreSQL、RabbitMQ、产物使用持久化卷。
- 只有前端映射到 `127.0.0.1:8180`，数据库、消息队列和 API 无宿主机公开端口。
- 现有 HTTPS Nginx 引入 `deploy/nginx-eyes.conf`；转发时剥离 `/eyes/` 前缀。前端构建基址和 BrowserRouter 为 `/eyes/`，FastAPI root_path 为 `/eyes/api`。

部署账号 `eyes-deploy` 属于 Docker 组，属于高权限运维账号。仓库 Secrets `EYES_DEPLOY_SSH_KEY` 和 `EYES_DEPLOY_KNOWN_HOSTS` 分别存放专用私钥和已核对的主机公钥。GHCR 拉取使用当次 Actions 的短期 GITHUB_TOKEN，结束时 logout。不要在日志或 Git 中保存密钥。

## 部署顺序与失败处理

部署使用文件锁串行执行：拉取镜像 → 启动依赖 → 备份已有数据库 → Alembic upgrade head → 更新 API/后台进程/前端 → 本机 ready 检查 → 更新 current → 公网 ready 检查。迁移失败会停止流程；更新后的健康检查失败会令 Actions 失败，不会自动回滚数据库。Compose 更新有短暂中断，不保证零停机。

此前成功部署目录和数据库备份保留。需要回退时先确认数据库迁移向后兼容，再在对应 release 目录运行 `bash release.sh`；不兼容迁移须人工恢复数据库及一致的产物卷。自动备份只覆盖 PostgreSQL，完整灾备还须保存 artifacts 卷；本流程不会自动删除备份或数据卷。

## 首次使用

首次部署后用 `eyes-admin bootstrap --name Eyes` 创建项目及管理令牌，输出保存到服务器仅管理员可读文件。控制台需要令牌连接，未认证请求不能读取项目数据。Runner 应单独安装在目标 Agent 所在机器，通过 `https://www.mneme.com.cn/eyes/api` 连接并使用 Runner 专用令牌。云端部署控制面不会自动安装或运行用户的 Agent。

## 运维

```sh
ssh tencent
cd /opt/eyes/current
docker compose --project-name eyes --env-file /opt/eyes/.env --env-file images.env -f compose.yaml -f compose.cloud.yaml ps
curl --fail https://www.mneme.com.cn/eyes/api/health/ready
```

Compose 需要 >=2.24.4（云端覆盖文件使用 `!reset` / `!override`）。部署脚本使用 Bash、flock、curl；服务器需安装 Docker Engine 与 Compose。`workflow_dispatch` 可以重试最新 main；不要通过修改 `latest` 标签绕过按提交部署。
