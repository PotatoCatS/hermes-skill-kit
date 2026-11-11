# 日常操作

所有命令都在 WSL 中执行。数据目录约定：

```bash
SKM=~/.hermes/skill-management
KIT=~/.hermes/skill-kit
```

## 1. 看板

打开 <http://localhost:8000>。

| 页面 | 用途 |
| --- | --- |
| 总览 | 技能计数、来源分布、产生方式分布、审计摘要、常用技能 |
| 技能清单 | 按分类/状态/来源/产生方式筛选，点击行看详情 |
| 审计 | 质量、安全、重复、更新四类结果，可按技能筛选、对比历史 |
| 审批中心 | 待批提案，dry-run、批准、执行、回滚 |
| 设置 | 修改密码、查看数据目录 |

常用技能：在清单里点星标，或打开技能详情后「加入常用」。上限 12 条，可写 200 字备注、上下移动排序、复制调用提示。

## 2. 盘点

```bash
cd "$SKM"
python3 scripts/scan_skills.py            # 生成 registry/skills.json 与 registry/skill-inventory.json
python3 scripts/compare_inventory.py --new registry/skills.json
```

盘点全程只读：文件只以二进制只读方式打开计算哈希，不写入、不执行。

## 3. 审计

四类审计由 Hermes 侧的 `skill-lifecycle-manager` 技能驱动（看板只展示结果）：

- 质量：七维评分（满分 100），每维必须附证据；
- 安全：10 项检查，Critical/High 必须给文件+行号+原文；
- 重复：候选对 + 六维证据，分 Duplicate / High Overlap / Partial / Complementary / Independent；
- 更新：不联网、不猜上游，只分「已确认 / 未知 / 不适用」。

规则正本在 `pipeline/config/`，报告写入 `audits/<类别>/`。

## 4. 提案与审批

1. 复制 `pipeline/proposals/TEMPLATE.yaml`，填 `problem`、`evidence`、`proposed_changes`、`diff`；
2. 校验：

   ```bash
   cd "$SKM"
   python3 scripts/proposal_lint.py proposals/pending/SKM-P-YYYYMMDD-NNN.yaml
   ```

3. 在审批中心打开提案，先看 diff，再 dry-run；
4. 用一句包含「批准 + 提案号」的话批准（多技能提案必须逐个点名）；
5. 执行：先备份到 `backups/<change_id>/`，再按 diff 写入，最后跑结构化与哈希校验；
6. 失败或效果不对，用回滚：

   ```bash
   python3 scripts/rollback_change.py <change_id>
   ```

7. 旧备份按保留策略清理：

   ```bash
   python3 scripts/cleanup_backups.py --keep 10
   ```

边界：`registry/`、`config/`、`cron` 等受保护文件不会被回滚覆盖；原始技能在未经批准前永远只读。

## 5. 定时任务（可选）

安装脚本不创建定时任务；需要时按 `pipeline/config/cron-prompts/` 里的六份提示词在 Hermes 侧手动创建。任务默认只投递到本地（`--deliver local`），不会外发。检查运行情况：

```bash
hermes cron list
hermes cron runs <name>
```

## 6. 更新与日志

```bash
cd "$KIT" && sudo bash update.sh --pull
journalctl -u hermes-skill-dashboard -f
tail -f "$SKM/logs/scan-log.md"
```

看板的操作审计会记录批准、拒绝、执行与回滚，用于事后追溯；它不记录密码、Cookie 与会话密钥。

## 7. 备份建议

需要迁移时，复制这两个目录即可恢复全部状态：

```bash
~/.hermes/skill-management
~/.hermes/skill-dashboard-data
```

代码目录可以随时从仓库重新克隆。
