# pipeline

技能生命周期管线的代码与规则。脚本只依赖 Python 标准库与 PyYAML，可直接在系统 Python 下运行。

## 脚本

| 脚本 | 作用 | 主要参数 |
| --- | --- | --- |
| `scan_skills.py` | 扫描技能目录，生成 registry 与 inventory；只读 | `--skills-dir` `--bundled-dir` `--out-dir` `--hermes-home` |
| `compare_inventory.py` | 对比两次盘点，输出增减与哈希变化 | `--old` `--new` `--out-dir` |
| `proposal_lint.py` | 校验提案字段、SHA-256、diff 范围与审批语句 | `<proposal>` `--print-diff-sha256` |
| `apply_proposal.py` | 备份后按批准 diff 落盘并校验 | `<proposal>` `--hermes-home` `--base-dir` |
| `verify_changes.py` | 变更后核对哈希、结构与目标范围 | `--change-id` |
| `rollback_change.py` | 按备份恢复目标文件 | `<change_id>` |
| `cleanup_backups.py` | 按保留数量清理旧备份 | `--keep` `--base-dir` |

所有路径参数默认取 `HERMES_HOME` 环境变量，未设置时用 `~/.hermes`；管线数据默认在 `~/.hermes/skill-management`。

## 规则

- `config/category-rules.md`：分类与来源判定（builtin / builtin-shadow / local）；
- `config/audit-rules.md`：质量评分七维与等级；
- `config/security-rules.md`：安全审计 10 项与风险等级；
- `config/cron-prompts/`：定时任务提示词（可选）；
- `config/manager-config.yaml.in`：管理配置模板，安装时渲染为 `manager-config.yaml`。

## 数据目录

安装后脚本从 `~/.hermes/skill-management` 运行，产物写在同目录：

```
registry/     skills.json / skill-inventory.json
reports/      daily / weekly / monthly
audits/       quality / security / duplication / updates
proposals/    pending / approved / rejected
logs/         scan-log.md / change-log.md / error-log.md
backups/      每次变更的完整备份与 MANIFEST
```

这些目录都不进 git。仓库里的 `examples/` 只是脱敏演示数据。

## 示例

```bash
SKM=~/.hermes/skill-management
cd "$SKM"

python3 scripts/scan_skills.py
python3 scripts/compare_inventory.py --old registry/skills.json --new registry/skills.json
python3 scripts/proposal_lint.py proposals/pending/SKM-P-EXAMPLE.yaml --print-diff-sha256
```

## 安全

被扫描的技能属于不可信输入：不要执行其中的脚本，不要把真实密钥写进提案或报告。变更必须先批准、先备份，回滚只覆盖提案登记的文件。
