# README 视觉资产提示词

制作方式：宣传图使用内置 imagegen；架构图使用原生 SVG 绘制。用户示例仅用于视觉方向参考，未复制其内容或品牌。

## 宣传图

文件：`docs/assets/multiclaw-capabilities.png`

```text
Use case: ads-marketing / infographic-diagram.
Asset type: wide GitHub README capability poster for MultiClaw, landscape 16:9, high-resolution, readable at 900 px width.
Create an ORIGINAL polished Chinese technology infographic. Match the visual direction of the user's first reference: near-white ice-blue background, four vivid rounded feature columns with blue/teal/orange/purple headings, attractive editorial flat/isometric illustrations, precise connector arrows and a sophisticated bottom technology platform. Do not copy its artwork, existing logos, its wording, or AI Gateway product claims. No watermarks.
Top title exactly: "MultiClaw". Subtitle exactly: "可协作 · 可恢复 · 可审阅的 AI Agent 运行时".
Four large feature cards across the middle, each has a distinct meaningful illustration and these exact Chinese labels, large crisp typography:
BLUE card heading "多 Agent 协作"; items "独立子 Agent" / "2–6 人 Agent Team" / "任务依赖与成员消息". Illustration: one leader and multiple collaborator nodes with task cards.
TEAL card heading "持久化工作流"; items "计划审核与检查点" / "后台运行与重启恢复" / "进度重连与补充指令". Illustration: approved checklist, checkpoint timeline and continuation arrows.
ORANGE card heading "隔离写入与交付"; items "独立 Git worktree" / "Diff 审阅与确认" / "确认后应用到主项目". Illustration: isolated code branches converging only through review gate.
PURPLE card heading "租户安全与治理"; items "租户 / 工作区作用域" / "高风险操作审批" / "Secret 加密与原生沙箱". Illustration: shield, lock, tenant partition tiles.
Bottom visual platform anchors and exact labels: "模型路由" / "内置工具与 MCP" / "SQLite 或 MySQL" / "React Web 控制台", connected to a central MultiClaw engine tile. A clean modest footer, exact: "单机部署 · 开发阶段 · 部分能力需显式启用".
Style: premium open-source launch graphic, generous spacing, clean Chinese sans-serif typography, luminous but restrained gradients, illustrated technical concept objects rather than fake product screenshots. Saturated accent colors on light canvas. Clear reading order, ample margins, no dense tiny copy, no faux 3D text, no random extra features. Do not imply clusters, Slack/Discord channels, SaaS billing, AI heartbeat agents, or production maturity. Render all provided text faithfully with no duplicated cards or malformed characters.
```

## 分层架构图

文件：`docs/assets/multiclaw-architecture.svg`

实现规格：1800 × 1260，深蓝背景、五种层级强调色、发光边框与玻璃质感节点；
左侧 TenantContext 作用域，右侧 EventRouter/SSE 与独立检查点恢复通道，底部显式
Writer 审阅交付流程。SVG 自包含，不使用脚本、外部字体或远程资源，包含替代
文本和描述。中文标签直接绘制为文字，图中的模块关系以当前项目文档为准。

下面保留两次因网络失败而未产出图片的 imagegen 提示词，供追溯视觉方向；
它不是架构 SVG 的生成方式。

```text
undefined
```
