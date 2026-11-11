# Skill 分类规则（category-rules.md）

> 版本：1.1（2026-11-02）
> 用途：盘点脚本对 Skill 分类的统一依据。只做分类标注，不改动 Skill 本身。

## 分类来源（优先级从高到低，与实现一致）

1. frontmatter 顶层 `category` 字段（如有）；
2. `metadata.hermes.category`（内置技能普遍使用）；
3. 由 `metadata.hermes.tags` 推断（如 software-development 类标签）；
4. 路径第一段回退：`skills/<分类>/<技能>/SKILL.md` → 分类 = 第一段路径。
   依据：Hermes 加载器 `tools/skills_tool.py` `_get_category_from_path`（相对路径 parts ≥3 时取 parts[0]），
   与加载器行为保持一致；
5. 仍无法确定 → 归入「未分类」，由人工裁定。

## 分类键（盘点聚合用）

autonomous-ai-agents / creative / devops / email / media / note-taking /
productivity / research / social-media / software-development /
troubleshooting / web / 未分类

## 来源判定（source_type）

由 scan_skills.py 与内置技能源（hermes-agent/skills/）比对得出：

- `builtin`：与内置技能源中同名技能的清单哈希完全一致（未被本地修改）；
- `builtin-shadow`：与内置技能同名但内容不同 —— 本地同名技能遮蔽内置版本，
  或内置技能被本地修改，仅凭文件内容无法进一步判定；
- `local`：内置技能源中不存在同名的本地技能。

判定方式为清单 SHA-256 比对，只读，不改动任何文件。

## 补充标注（不改变主分类）

- 来源：builtin / builtin-shadow / local（见上）
- 加载状态：confirmed（确认加载）/ gated_by_platform（platforms 不含当前平台，Hermes 门控不加载）
- 平台：linux / macos / windows（来自 frontmatter `platforms`）
- 状态：与内置 Curator 对齐 —— active / stale / archived（.archive 归档区单独登记，不计入已安装清单）

## 规则

- 分类只记录，不修改原 SKILL.md。
- 分类变更提案走 proposals/pending/，用户批准后才能落地。
- 扫描对被扫描目录完全只读：文件仅以二进制只读方式打开计算哈希，不写入、不执行。
