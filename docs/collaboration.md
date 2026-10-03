# 子 Agent 与 Agent Team

每个分派任务拥有独立内部会话、Run、模型上下文、工具配置、用量和检查点。任务句柄立即返回，执行由后台调度器拥有；关闭浏览器不取消任务。内部会话不进入普通会话列表。

## 启用

先执行 `uv run multiclaw db upgrade`，迁移到 `20261002_0003`；再设置 `[collaboration] enabled=true` 或 `MULTICLAW_COLLABORATION__ENABLED=true` 并重启服务。字段见[配置参考](configuration.md#协作collaboration)。聊天模型会获得 `spawn_agent`、`agent_status`、`agent_message`、`agent_cancel`、`agent_wait`、`create_team`、`team_status`，由模型选择调用。也可在会话的 Collaboration 面板显式创建。

旧的 `agent.subagents_enabled` 只开放父 Run 内同步等待的只读 `delegate_tasks`，与新功能分别控制。

## 独立子任务

Reader 开放本地读取和搜索。Writer 还可在自己的 worktree 中写文件、执行沙箱 Shell 和代码；变更工具继续审批，审批不能扩大成员工作区。成员不获得 MCP、网络抓取、跨工作区文件权限或再委派工具。成员只接收分派目标、显式上下文、适用规则和后续指令，不继承完整父对话。

`project_path` 相对租户工作区解析。Writer 首版要求干净 Git 仓库根目录，拒绝非 Git、嵌套仓库、越界和符号链接逃逸。模型只能选择默认项或 `llm.model_providers` 中的项。

面板可查看状态、最近进度、对话、用量和审批，发送补充指令或取消。指令进入该成员的持久化对话，审批等待与重启后仍可读取。重启后使用原 Run 检查点继续；非幂等执行结果不确定时沿用人工恢复边界。

## 团队

团队包含一个 leader 和至少一个 member，最多六人。成员身份与配置持久化，每次分派产生独立 Run。leader 创建带依赖的任务，调度器通过版本检查领取就绪任务，每个成员同时执行一个分派。成员使用 `team_board`、`team_inbox`、`team_message` 查询与通信；`team_create_task` 再次校验 leader 身份。任务完成后 leader 总结。用户也可在面板创建任务、指定成员、发送消息和取消团队。

所有记录限定于创建它的 tenant/workspace/session，成员、依赖和消息关系受作用域校验。每会话默认最多 100 个分派；固定上限为 20 个团队，每团队 100 个任务、500 条消息。有界游标扫描会越过等待审批的任务。

每个分派创建时分配 Token 上限；父 Run 可用额度扣除其分派额度，团队累计受 `max_team_tokens` 约束。保守分配不会因取消而返还。实际模型用量仍按各 Run 和租户日额度记账，并发也受现有租户 Run 配额约束。

## 变更交付与清理

成员写入不改变主工作区。单独子任务完成后可审阅并接受 diff；团队使用整体审阅入口，一次接受 Writer 的不重叠文件改动。digest 绑定实际隔离快照，修改后须重新审阅。重叠文件、基线移动、主工作区其他变更、受保护路径和超限 diff 会被拒绝，产物保留。

接受不自动提交或推送。团队应事先划分文件所有权；重叠改动在隔离工作区解决后重新审阅。服务内操作串行化，接受期间外部编辑器仍应停止修改项目。`.multiclaw` 运行产物不会进入补丁。

正常完成保留隔离产物。删除会话时先停止分派，再删除经过验证的 worktree 与持久化记录；账号清除也删除租户隔离数据。
