---
name: skill-creator
description: 根据用户描述生成符合 Anthropic Agent Skills 规范的技能（SKILL.md）。当用户要求"写一个技能 / 新建 skill / 帮我做一个 XX 技能"时使用，产出一个结构完整、命名规范、可直接落盘的 SKILL.md。
---

# Skill 生成规范（Skill Creator）

当你被要求创建一个新技能（skill）时，按本规范生成一个完整的 `SKILL.md` 文件内容，并写入 `.deepagents/skills/<skill-name>/SKILL.md`。

## 1. 命名规范

- 目录名 / frontmatter `name` 必须一致，使用小写字母、数字、连字符（如 `web-research`、`pdf-summary`），最长 64 字符
- 名称要能一眼看出用途，不要用通用词（`utils`、`helper`）
- 描述（`description`）一句话说明"何时使用 + 做什么"，供 Agent 路由判断

## 2. 文件格式（必须）

```markdown
---
name: <skill-name>
description: <一句话描述：何时使用、做什么>
---

# <技能名>

## 何时使用
（触发该技能的场景，明确、可判断）

## 使用方式
（核心步骤：输入什么、如何执行、输出什么）

## 示例
（1-2 个可直接套用的示例，含输入与期望输出）

## 注意事项
（边界、易错点、禁止行为、依赖要求）
```

- 必须带 YAML frontmatter（`---` 包裹的 name/description），无 frontmatter 的技能会被面板识别为缺失描述
- 正文用 Markdown，标题层级从 `#` 开始，善用列表与代码块
- 内容具体可执行，禁止空泛套话；涉及文件操作要给出明确路径

## 3. 放置路径

- 唯一合法位置：`.deepagents/skills/<skill-name>/SKILL.md`（项目根下的 .deepagents 目录）
- 生成后立即检查文件存在且内容正确

## 4. 质量要求

- 描述与正文一致，不自相矛盾
- 步骤可被模型直接执行，不依赖人工补充
- 若技能需要特定依赖/环境，在"注意事项"里写明
- 生成后向用户汇报：技能名、路径、一句功能说明

## 5. 禁止

- 不要生成不在 `.deepagents/skills/` 下的技能文件
- 不要修改或覆盖已有技能（除非用户明确要求）
- 不要使用大写、空格、中文/特殊字符作技能名
