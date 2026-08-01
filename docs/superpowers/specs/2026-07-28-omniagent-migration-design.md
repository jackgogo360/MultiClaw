# OmniAgent 优秀能力迁移设计

**日期**：2026-07-28

**状态**：Phase 0/1 engineering implementation complete; credential rotation pending operator action

**范围**：Phase 0 + Phase 1 洁净室重实现；Phase 2/3 未启动

**来源（仅审计阶段）**：`/Users/felix/git/OmniAgent`、`README_CN.md`、官网本地源码 `index.html`

## 结论摘要

建议采用**洁净室、分阶段重实现**：把 OmniAgent 的公开行为、交互理念和经过验证的算法思路整理为规格，在 MultiClaw 现有架构中重新实现，不直接复制源码。首批只迁移高收益、边界清晰的能力：卡死检测与反思重试、受限的只读工具并发、渐进式上下文预算、SSRF 防护。主动/分层记忆放在第二阶段；Sentinel、Guardian 和自进化必须改成“静态策略优先、生成变更需审批”；RL 与自动生成可执行技能暂缓。

主要原因：OmniAgent 使用 GPL-3.0，而 MultiClaw 当前未声明许可证；直接复制可能使派生作品承担 GPL 合规义务。OmniAgent `main` 缺少测试，部分官网能力是实验性、未合并分支功能，或与实现存在明显差距。本文不是法律意见，正式分发前仍应确认许可证策略。

## 实施证据

- 已按策略 C 在隔离工作树中重实现 Phase 0 + Phase 1，未复制 OmniAgent GPL 源码；新增运行时能力默认关闭，WebFetch 默认禁止访问私网。
- 后端全量验证：`uv run pytest -q`，结果为 `292 passed, 12 warnings`。警告均为既有 aiosqlite 连接线程在事件循环关闭后的清理问题。
- 前端验证：`npm run lint` 和 `npm run build` 均以状态码 0 完成；构建仅报告既有的大 chunk 提示。
- 前端基线与 Task 10 已分别通过规格和质量审查；最终分支级审查在合并前执行。
- 已知残余风险：DNS 解析与实际连接间仍存在 rebinding/TOCTOU 窗口；凭据轮换与历史清理仍需操作者在外部系统完成；Phase 2/3 不在本次交付范围。

## 审计范围与可信度

- 审阅 OmniAgent `main`、远端功能分支、中文 README、官网源码、提交历史和 GPL-3.0 许可证。
- 审阅 MultiClaw 的 Agent 循环、工具调度、审批、治理、记忆、Planner、MCP、会话、SSE 与前端工具 UI。
- 官网在线地址在审计时连接被重置，因此官网功能对照以仓库根目录 `index.html` 为准。
- OmniAgent `main` 未发现成体系测试；`origin/feature/memory-system` 的 `c491c2f` 增加约 1,400 行记忆测试，但尚未合并，应视为实验性参考。

## 官网宣称与代码现实

| 能力 | 审计结果 | 迁移判断 |
|---|---|---|
| 依赖感知的并行工具执行 | 实际是按工具名区分“有副作用/可并行”，再用 `asyncio.gather`；没有真正的依赖图。见 `omniagent/agents/reflexion.py:1239`、`:1453`、`:1489`。 | 借鉴目标，不复制实现；先做有上限的只读并发。 |
| Guardian 四层安全 | 只在 critical 且配置允许时自动阻断；审查异常时继续执行。见 `reflexion.py:1381`、`:1420`。独立进程/容器沙箱未得到验证。 | 只保留“风险顾问”概念，静态策略必须拥有最终决定权。 |
| Sentinel 规划与里程碑验证 | 已有分解和验证结构，但主流程 `await` 了同步的 `mark_milestone_completed`，且 `verify_milestone` 未接入完成路径。见 `reflexion.py:535`、`sentinel.py:488`、`:537`。 | 暂不整块迁移；先升级 MultiClaw Planner，再引入可验证里程碑。 |
| 混合记忆检索 | 主分支把查询字符串传给期望向量的 `_search_vector`。见 `memory_manager.py:225`、`:245`、`:276`。 | 以测试更完整的功能分支为行为参考，重新设计接口。 |
| 技能自进化 | 可直接写入并 `chmod 755` LLM 生成的 shell 脚本。见 `skill_evolution.py:848` 附近；另有未合并缺陷修复 `6000617`。 | 禁止自动激活；只生成草案、差异和测试，必须审批。 |
| 上下文自进化 | 可自动向 `AGENTS.md` 追加规则。见 `context_evolution.py:226`、`:328`。 | 只能写提案文件，不能直接改顶层治理文件。 |
| RL 在线训练 | 只面向本地 vLLM/SGLang，并依赖 SLIME 生态。见 `reflexion.py:235` 附近。 | 与 MultiClaw 当前产品路径不匹配，暂缓。 |
| Discord/Telegram/飞书通道 | 文档宣称完整，但代码成熟度和端到端验证不足。 | 核心运行时稳定后再考虑通道抽象。 |

## MultiClaw 适配基础与前置缺口

MultiClaw 已有适合承载迁移的扩展面：`ToolBuilder`/`ToolInvocation`（`src/multiclaw/tools/base.py:28`、`:42`）、统一调度和审批、MCP 适配器、SSE 工具事件（`src/multiclaw/server.py:383` 起）及前端审批 UI（`frontend/src/components/approval/ApprovalToolUI.tsx:13`）。因此不需要移植 OmniAgent 的整体 Agent 框架。

实施前必须先处理以下基线问题：

- 工具调用在同步与流式路径中均串行执行（`agent/multiclaw.py:148`、`:323`）。
- Planner 仅按英文 `" and "` 拆分文本（`planner/planner.py:6`）。
- SQLite 记忆只做词项重叠排序（`memory/sqlite.py:82` 附近）。
- 审批等待状态只存内存（`tools/scheduler.py:25`），重启后丢失。
- `ProcessSandbox` 只是超时包装（`governance/sandbox.py:13`），不能称为隔离沙箱。
- `web_fetch` 跟随重定向，但未校验私网、环回、链路本地地址（`tools/web_fetch.py:55`、`:133`、`:165`）。
- 会话恢复只重建文本 part，工具、推理和审批轨迹会丢失（`frontend/src/components/session/SessionProvider.tsx:61` 起）。
- MCP 适配器在异步 `execute()` 中直接调用同步桥接，可能阻塞主事件循环；动态刷新也不会更新工具注册表（`mcp/tool_adapter.py:163`、`mcp/manager.py:196`）。采用 MCP sidecar 前必须修复。
- 审计中发现配置存在疑似凭据值；本文不记录其路径或内容。任何共享、开分支或发布动作前应完成密钥扫描、轮换和历史清理评估。

## 候选功能优先级

| 优先级 | 能力 | 收益 | 工作量 | 主要风险/依赖 |
|---|---|---:|---:|---|
| P0 | 许可证决策、密钥处置、基线测试、功能开关 | 必需 | S | 决定后续能否安全开发和分发 |
| P1 | 卡死/重复错误检测与反思重试 | 高 | M | 需避免误判和无限重试 |
| P1 | 有界只读工具并发 | 高 | M | 副作用分类、结果顺序、取消和审批 |
| P1 | SSRF 防护与重定向复检 | 高 | M | DNS 重绑定、浏览器模式也需覆盖 |
| P1 | L0/L1/L2 渐进式上下文预算 | 高 | M | Token 估算、降级和可观测性 |
| P2 | 主动/短期/长期分层记忆 | 高 | L | 以 `c491c2f` 行为为参考；需租户隔离与质量评测 |
| P2 | 持久化工具/推理/审批轨迹 | 高 | L | 会话 schema 和前端 hydration 迁移 |
| P2 | 策略配置档与持久审批 | 中高 | M | 静态策略优先，跨进程一致性 |
| P3 | Sentinel 可验证里程碑 | 中 | L | 先替换当前 Planner；每步需明确验收证据 |
| P3 | Guardian 风险顾问 | 中 | M | LLM 不得降低静态风险等级；故障不能放行高风险操作 |
| P3 | 技能/上下文进化提案 | 中 | L | 只产出 patch、测试和说明，人工批准后才能应用 |
| P4 | 多通道适配层 | 中 | L | 身份映射、权限、附件、限流和重放 |
| 暂缓 | RL、自动生成并执行脚本 | 低/不确定 | XL | 依赖重、攻击面大、与当前路线不匹配 |

工作量口径：S 为 1–3 个工程日，M 为 4–8 个工程日，L 为 9–15 个工程日，XL 需单独立项；估算不包含产品验收和安全审计。

## 三种迁移策略

### A. 直接复制源码

速度最快，但默认不采用。风险包括 GPL 合规、主分支缺少测试、已知缺陷随代码进入 MultiClaw，以及把 OmniAgent 的单体循环强行嵌入现有架构。

### B. GPL 隔离 sidecar / MCP 服务

将 OmniAgent 作为独立 GPL 进程，通过 MCP 或 HTTP 提供能力。边界比源码混合清晰，但仍需法律审阅；同时 MultiClaw 当前 MCP 适配层须先修复，部署、鉴权、故障恢复和双重状态管理成本较高。适合未来需要保留 OmniAgent 原实现时评估。

### C. 洁净室增量重实现（推荐）

审计者只输出行为规格、风险和黑盒测试；后续使用未读取 OmniAgent 源码的新执行会话，根据本文和公开文档在 MultiClaw 内实现。每个能力单独开功能开关、单独验收、可独立回滚。该路线最符合现有模块边界，也便于修正 OmniAgent 的实现缺陷。

## 推荐架构

1. **Agent 韧性层**：在 `src/multiclaw/agent/` 增加独立的 loop guard/reflection policy，输入标准化工具调用和结果摘要，输出继续、反思、降级或终止决定；不把逻辑继续堆进 `multiclaw.py`。
2. **工具执行计划层**：在调度器前生成执行批次。默认串行；只有显式标记为只读、无审批、无共享写状态的调用可进入有并发上限的批次。保持原始 tool-call 顺序回填结果，并支持超时、取消和部分失败。
3. **上下文预算层**：新建 `src/multiclaw/context/`，定义 L0 摘要、L1 相关片段、L2 原文三档；每次注入记录来源、字符/Token 预算和淘汰原因。
4. **记忆层**：保留 `MemoryProtocol`，增加主动记忆、短期晋升和可插拔检索后端；写入按 `tenant_id`、`session_id` 隔离，向量不可用时可靠降级为 FTS/词法检索。
5. **治理层**：静态权限与网络/路径策略为权威层；Guardian 只能提高风险或建议审批，不能放宽策略。审批、审计和工具轨迹持久化到 SQLite。
6. **进化层**：只写入隔离的 proposal 目录，产物包含来源证据、patch、回归测试、风险说明和撤销方法；禁止直接修改 `AGENTS.md` 或生成后自动执行脚本。

## 分阶段迁移与验收

### Phase 0：安全与基线

- 明确采用 A/B/C 中哪种许可证路线；推荐 C。
- 处理疑似凭据，记录轮换结果；为后续能力增加默认关闭的 feature flags。
- 修复会阻断迁移的 MCP 事件循环阻塞/动态注册问题，并为 Agent、scheduler、memory、session 建立回归测试基线。
- 验收：`uv run pytest`、`npm run lint`、`npm run build` 通过；现有聊天、工具调用、审批、会话恢复行为不回退。

### Phase 1：高收益运行时能力

- 实现重复工具、重复参数、重复错误和无进展检测；重试次数有硬上限，反思上下文可观测。
- 只并发执行显式只读调用，默认并发上限 4；写工具、shell、审批工具和未知工具保持串行。
- `web_fetch` 在首次请求和每次重定向后阻断环回、私网、链路本地、保留地址及非 HTTP(S) 协议；浏览器模式使用相同策略。
- 实现 L0/L1/L2 上下文预算与超限降级。
- 验收：并发结果仍按原 tool-call ID/顺序呈现；取消不会遗留任务；SSRF 测试覆盖 IPv4、IPv6、DNS/重定向；卡死场景能在上限内改变策略或终止。

### Phase 2：记忆与可恢复轨迹

- 先持久化完整消息 part、工具结果、推理摘要和审批状态，再引入主动/短期/长期记忆。
- 使用固定评测集比较词法、FTS 和可选向量检索；向量服务失败时结果可预测、无异常。
- 验收：重启后会话 UI 与执行轨迹一致；不同租户/会话无串读；短期记忆晋升、淘汰和冲突更新均有确定性测试。

### Phase 3：受控规划、安全顾问与进化

- 以结构化 DAG/里程碑替换字符串拆分 Planner；每个里程碑绑定可执行验证器，而不是仅让 LLM 自评。
- Guardian 输出风险、理由和建议，但静态策略拥有最终裁决权；Guardian 超时/异常时，高风险调用进入拒绝或人工审批，不得静默放行。
- 技能和上下文进化仅生成提案；用户在现有审批 UI 查看差异、测试和风险后决定应用。
- 验收：计划恢复后不会跳步；验证失败会回到对应里程碑；未经批准工作区无变化；提案应用失败可原子回滚。

## 明确不迁移的内容

- 不复制 OmniAgent 的 `reflexion.py` 主循环或整套目录结构。
- 不把 `ProcessSandbox` 或命令正则包装宣传为真正的进程隔离。
- 不启用自动写 `AGENTS.md`、自动生成并执行 shell 技能、自动修改安全策略。
- 不在首轮引入 SLIME、vLLM/SGLang 在线 RL、未验证的 Discord/Telegram 适配器。
- 不以官网文案作为完成证据；所有能力以测试和运行时可观测结果验收。

## 风险与控制

| 风险 | 控制措施 |
|---|---|
| GPL 或来源污染 | 采用规格/实现分离；新执行会话不得读取 OmniAgent 源码；保留来源和决策记录。 |
| 并发引入竞态 | 默认串行、只读显式 opt-in、有限并发、稳定排序、共享资源锁。 |
| LLM 安全判断不稳定 | 静态策略优先；LLM 只能升级风险；高风险故障时拒绝或审批。 |
| 记忆污染或跨租户泄漏 | 强制 tenant/session scope、来源元数据、删除/过期路径和隔离测试。 |
| 自进化破坏治理 | 只生成提案；差异、测试、审批、原子应用和回滚缺一不可。 |
| 范围膨胀 | 每阶段独立 feature flag 和验收门；Phase 1 未稳定前不启动 Phase 2。 |

## 已确认决策

1. 采用策略 C（洁净室增量重实现），实现阶段不再读取 OmniAgent 源码。
2. 首批范围为 Phase 0 + Phase 1；Phase 2/3 需要单独设计、批准和验收。
3. `origin/feature/memory-system@c491c2f` 只可作为未来 Phase 2 的行为研究材料，不直接复制。
4. 自进化保持“提案 + 人工审批”；RL 和自动生成并执行技能继续暂缓。
