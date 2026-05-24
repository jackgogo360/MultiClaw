# Skill System Design

**Date:** 2026-05-23
**Status:** draft

参考 `MyObsidianVault/agent-code/20260522-skill-system` 实现，为 MultiClaw 增加 skill（技能/插件）功能。

## 概述

Skill 系统允许用户通过 `SKILL.md` 文件定义可复用的 agent 能力扩展。每个 skill 是一个包含 `SKILL.md` 的目录，支持 keyword 和 manual 两种触发模式，采用三级渐进式披露控制 token 消耗。

## 模块结构

```
src/multiclaw/skills/
├── __init__.py       # 公开 API
├── types.py          # Skill, SkillMetadata, Trigger, TriggerType, DisclosureLevel
├── parser.py         # SKILL.md frontmatter 解析 + 渐进式加载
├── discovery.py      # 多层目录扫描
├── activation.py     # 触发匹配 + 激活/去激活 + 参数替换
└── manager.py        # SkillManager 统一入口
```

模块从参考实现适配而来，主要改动：
- import 路径改为 `multiclaw.skills.*`
- 去掉 `always` 触发模式
- discovery 同时支持 `.multiclaw/skills/` 和 `.agents/skills/` 两套目录

## SKILL.md 格式

```markdown
---
name: my-skill
description: 一句话描述
triggers:
  - type: keyword
    keywords: [test, testing]
inputs: [target, scope]
paths: ["*.test.ts", "tests/**"]
allowed_tools: [Bash, Read, Write]
max_tokens: 4000
version: "1.0"
tags: [testing]
---

skill 指令内容。使用 $ARGUMENTS 获取完整参数，$target 获取命名参数。
```

手动触发（manual）的 skill 不需要声明 `keywords`；不声明 triggers 则默认为 manual。

## 目录发现

扫描顺序（后者覆盖前者 by name）：

1. 用户级：`~/.multiclaw/skills/` 和 `~/.agents/skills/`
2. 项目级：`.multiclaw/skills/` 和 `.agents/skills/`（从项目根向上遍历到 HOME）
3. 额外目录：配置中指定的 `extra_dirs`

两个目录系列的优先级相等（同级按字母序扫描，后发现的覆盖）。两者都支持是为兼容不同生态的 skill 目录约定。

## 三级渐进式披露

| 级别 | 内容 | 场景 |
|------|------|------|
| METADATA | 名称 + 描述 | 列出可用 skill |
| INSTRUCTIONS | 完整 body 指令 | 激活后注入 prompt |
| RESOURCES | body + 资源文件清单 | 需引用 scripts/references/assets |

激活时自动从 METADATA 提升到 INSTRUCTIONS；`load_skill_resources()` 按需进一步提升到 RESOURCES。

## 触发模式

- **keyword**: 用户消息包含声明关键词时自动激活
- **manual**: 用户以 `/skill-name args` 格式显式调用

去掉参考实现中的 `always` 模式（按用户要求暂不支持）。

## 集成架构

### Agent 持有 SkillManager

`MultiClawAgent.__init__` 中创建 `SkillManager(project_root=workspace_root, max_active=...)`，启动后调用 `manager.discover()` 扫描所有 skill。

### ContextBuilder 注入 skill prompts

`ContextRequest` 新增 `skill_prompts: list[str]` 字段（默认空列表）。`build()` 在 system prompt 之后、history 之前，为每个 skill prompt 插入一条独立 system 消息：

```python
{"role": "system", "content": f'<skill name="{name}">\n{body}\n</skill>'}
```

### 消息处理流程

```
user_message 到达 handle_message / handle_message_stream
  │
  ├─ 以 "/" 开头?
  │   └─ 解析 /skill-name args
  │      └─ manager.invoke(name, args) → skill body with $ARGUMENTS substitution
  │         └─ 将结果加入 skill_prompts，yield {"type": "skill", ...}
  │
  └─ 普通消息
      └─ manager.process_message(user_input) → keyword matching
         └─ 新增激活的 skill 加入 skill_prompts
            └─ yield {"type": "skill", ...} for UI awareness
```

### SSE 事件

manual 调用和 keyword 激活时发送：

```json
{"type": "skill", "name": "my-skill", "active": true}
```

前端可用于展示当前激活的技能列表。

## 配置

`multiclaw.toml` 新增可选段：

```toml
[skills]
enabled = true
max_active = 5
extra_dirs = []
user_dir = ""
```

`Settings` 新增 `SkillSettings` model，所有字段有默认值。`enabled = false` 可完全禁用 skill 系统。

## 错误处理

- 格式错误的 SKILL.md：跳过并 `logger.warning`，不阻塞其他 skill
- 目录不存在：静默跳过
- 同名 skill：后发现的覆盖先发现的（project > user 优先级）
- 解析/IO 异常：全部 catch + log warning，不影响 agent 正常运行

## 测试计划

- `tests/test_skills.py` — 单元测试：
  - Parser：frontmatter 解析、渐进式加载
  - Discovery：多目录发现、同名覆盖、目录不存在容错
  - Activation：关键词匹配、manual 调用、参数替换
- 集成测试：验证 skill 注入到 ContextBuilder 消息序列的正确位置
