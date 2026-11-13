---
name: Eyes
description: Agent 观测与评测工作空间的任务索引
colors:
  sidebar: "#fafafa"
  sidebar-foreground: "#18181b"
  sidebar-primary: "#eaeaed"
  sidebar-primary-foreground: "#18181b"
  primary: "#18181b"
  primary-foreground: "#fafafa"
  link-color: "#2563eb"
  canvas: "#ffffff"
  surface: "#ffffff"
  ink: "#18181b"
  text-muted: "#65656f"
  line: "#e4e4e7"
  input: "#8a8a93"
  success: "#166534"
  warning: "#854d0e"
  info: "#1d4ed8"
  destructive: "#b91c1c"
typography:
  headline:
    fontSize: "clamp(28px, 2.65vw, 38px)"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.035em"
  body:
    fontFamily: '"DM Sans Variable", -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    lineHeight: 1.6
  record-title:
    fontSize: "14px"
    fontWeight: 600
    lineHeight: 1.55
  record-number:
    fontFamily: '"IBM Plex Mono", "SFMono-Regular", Consolas, monospace'
    fontSize: "13px"
  label:
    fontSize: "12px"
rounded:
  control: "0.5rem"
  run-state: "4px"
spacing:
  space-1: "4px"
  space-2: "8px"
  space-3: "12px"
  space-4: "16px"
  space-6: "24px"
  space-8: "32px"
  space-12: "48px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.control}"
  button-outline:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
  input:
    rounded: "{rounded.control}"
  navigation-current:
    backgroundColor: "{colors.sidebar-primary}"
    textColor: "{colors.sidebar-primary-foreground}"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.control}"
  execution-completed:
    textColor: "{colors.success}"
    rounded: "{rounded.run-state}"
  evidence-complete:
    textColor: "{colors.success}"
---
# Design System: Eyes

## Overview

**Creative North Star: "任务索引"**

白色画布承载任务表，浅灰侧栏标识当前位置。登录和注册使用居中的单栏表单。

**Key Characteristics:**

- 任务名称在前，来源、模型和会话在下一行。
- 时间与调用量采用等宽数字，执行结果与证据完整度各占一列。

## Colors

侧栏选中项使用灰色底，主要操作用近黑色实底。记录用浅灰细线分隔，输入框的描边更深。

状态文字随含义着色。绿色表示已完成或链路已收齐，蓝色表示运行中或采集中，琥珀色表示受限、中断或链路不完整，红色表示执行失败。

## Typography

正文与标题共用 DM Sans，中文由系统无衬线字体承接。侧栏与登录页沿用眼形标识和无衬线字标。正文标题靠字重分层，任务名称最多占两行。时间与计数用 IBM Plex Mono 和等宽数字，日期放在时刻下一行。

## Layout

桌面侧栏展开宽 14rem，收起时为 3.5rem。白色顶栏高 4rem，左侧显示当前位置，右侧是快速导航与同步状态。主区最大宽 100rem，左右内边距随视口在 16–40px 间变化。

任务索引将剩余宽度优先分给任务名称，时间、调用量及两种状态各自对齐。长列表可在表内滚动，表头固定在滚动区域顶部。窄屏保留表格的列关系，支持横向滚动。侧栏改用抽屉。

## Elevation & Depth

索引、表单和导航通过底色与细边线区分。任务表直接落在画布上。接入等模态对话框通过遮罩和外框与当前页面分层。

## Shapes

数据面板、输入框与常规按钮共用轻圆角。执行状态用圆角更小的浅色标签。证据状态用实心圆点和文字，背景透明。

## Components

- **导航**　当前项有浅底、深色图标和较重的文字。收起侧栏时保留图标和提示。顶栏的同步文字跟随连接状态。
- **任务行**　任务名链接到单次执行，会话链接筛选同一会话。表头与分页留在同一块白色面板内。
- **操作和表单**　接入、登录使用实底主按钮，快速导航使用描边按钮。表单保留可见字段名和键盘焦点轮廓。认证表单最大宽度为 360px，字段和按钮最小高度为 42px。登录时只显示必要字段和注册入口，注册时补充格式要求。
