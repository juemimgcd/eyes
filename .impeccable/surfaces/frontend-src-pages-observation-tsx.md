---
version: 1
slug: "frontend-src-pages-observation-tsx"
primary_target: "frontend/src/pages/Observation.tsx"
related_targets: ["frontend/src/App.tsx","frontend/src/components/AccountGate.tsx"]
---

# Eyes 任务索引

Mode: Operate

## Direction contract

THESIS: 从多条 Agent 任务中定位一次执行，再沿会话与事件追溯。每行代表一条真实记录。
OWN-WORLD: 白色画布、浅灰导航和近黑色主操作。登录页使用单栏表单，仅显示认证所需文字。任务名称采用中文无衬线，时间和调用量采用等宽数字。品牌沿用 Eyes 眼形标识。
STORY: 先比较任务和两种状态，再点任务看证据，或按会话筛出相关任务。首次使用从接入开始。
FIRST VIEWPORT: 左侧固定导航，顶栏保留快速导航和同步状态。主区标题右侧是接入入口；下方以任务及会话、记录时间、轮次及工具次数、执行状态、证据状态为列。
FORM: 任务索引，方向 7，seed:aeb5c28f6b7236d0。
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
