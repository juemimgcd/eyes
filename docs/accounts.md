# 个人账号与数据归属

用户在 Web 注册用户名和密码后自动获得一个独立项目。项目不是由浏览器提交或选择的，注册事务同时创建 Account 和 Project，登录后服务端从会话凭据取得 project_id。

- 用户名为 3–40 位 ASCII 字母、数字、下划线，可包含点和短横线；不区分大小写。密码为 12–128 个字符，使用 Argon2 哈希保存。
- 实验、批次、目标、测试集、评分器、观测来源及记录、回归报告、证据产物和实时通知沿用项目范围检查。跨项目记录按不存在返回 404。
- 会话仅保存随机令牌的 SHA-256 摘要；浏览器持有 HttpOnly、SameSite=Strict Cookie，默认 7 天有效。`EYES_SESSION_HOURS` 可设为 1–720 小时。会话令牌不写入 localStorage 或 URL。
- 退出撤销当前会话并清空页面查询缓存。多个标签页通过 BroadcastChannel 同步账号变化；请求携带当前账号标识，旧页面与新 Cookie 不匹配时返回 401。账号标识本身不授予权限。
- Cookie 写请求和登录、注册必须携带 `X-Eyes-Client: console`。API 不开放跨域 CORS；不要为任意来源放开带凭据的跨域访问。
- 登录与注册共用数据库限流，每个直连 peer 5 分钟最多 30 次，不信任客户端自行提交的转发 IP。反向代理未配置可信真实 IP 时，其后的用户共享该额度。
- 业务响应不缓存，SSE 定期复核会话撤销及过期状态。数据库备份恢复会撤销恢复出的浏览器会话。

## 升级

```sh
uv sync --frozen
uv run alembic upgrade head
```

当前迁移为 `0012_personal_accounts`，需要同时重启 API、调度和相关消费者并重新构建前端。迁移撤销旧 `read`/`manage` 共享凭据，旧共享项目不分配给任何个人账号，也不会由首个注册者继承。按本次要求不迁移旧数据；旧数据库记录和证据文件未物理删除，不应将升级当作数据擦除。降级不会重新启用旧令牌。

浏览器不再要求 `eyes-admin bootstrap` 或粘贴项目令牌。CLI/CI 可由主机管理员为账号的 project_id 签发 read/manage 凭据；Runner 与观测来源仍使用各自范围的凭据。旧 Runner/来源不自动转入新账号，需在新账号下重新接入。

免令牌本地观测默认关闭，避免本机自动上报被无意归入其他人的个人项目。常规接入使用账号下创建的来源令牌，见 [被动观测](observation.md)。

公网 HTTPS 设置 `EYES_SESSION_COOKIE_SECURE=true`；云部署 Compose overlay 已启用。仅本机 HTTP 开发使用 false。自助注册当前开放；本次未提供邮件验证、密码找回或账号管理后台。

## 接口

| 接口 | 用途 |
| --- | --- |
| `POST /v1/auth/register` | `{username, password}` 注册并创建会话 |
| `POST /v1/auth/login` | `{username, password}` 登录 |
| `GET /v1/auth/me` | 读取当前账号及会话过期时间 |
| `POST /v1/auth/logout` | 撤销当前会话，返回 204 |

注册不接收 project_id，不能据此认领已有项目。用户信息响应只包含 id、username、project_id 和会话过期时间。

## 本地验证（2026-11-06）

在独立 PostgreSQL 18.6 数据库完成从空库到 head 的迁移，`alembic check` 无模型差异。真实 HTTP 调用确认：注册两个账号得到不同项目；A 发布测试集后 B 的列表为空，B 请求 A 的详情得到 404；缺少浏览器写请求头返回 403；账号标识与会话不符返回 401；退出后重用旧会话返回 401。

桌面 Google Chrome（1440×1000、1280×900）验证注册、登录、错误密码提示、刷新恢复、退出、切换账号及个人数据目录，确认一个标签页退出后另一个标签页同步返回登录；未使用模拟 API。验证数据与服务在独立本地环境中，不是线上部署证据，也不替代真实 Agent 执行验收。没有创建或修改自动化测试文件。

检查结果：Ruff lint/format、TypeScript、ESLint、Vite build 与 git diff --check 通过。全量前端 Prettier 检查在未修改的 `AgentConnectDialog.tsx` 上报告原有格式问题；本次改动文件单独检查通过。
