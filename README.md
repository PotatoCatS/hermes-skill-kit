# hermes-skill-kit

Hermes Agent 的本地技能管理套件：一个只监听 `127.0.0.1` 的 Web 看板，加一套「扫描 → 审计 → 提案 → 批准 → 执行 → 验证 → 回滚」的技能生命周期管线。

目标是让「我装了哪些技能、它们是谁产出的、质量与安全如何、要不要改」这些问题有一个可查、可审、可回滚的答案，而不是靠记忆维护一堆 `SKILL.md`。

> **代码与数据分离**：仓库里只有程序、通用规则和脱敏示例。真实技能清单、审计报告、提案、日志、登录配置全部放在仓库之外，`git pull` 不会碰你的数据。

## 功能

- **盘点**：扫描 `~/.hermes/skills`，记录每个技能的路径、分类、版本、哈希、来源（builtin / builtin-shadow / local）、加载状态。
- **四类审计**：质量评分、安全风险、重复/重叠、更新状态，各自输出带证据的报告。
- **产生方式**：在「来源」之外再分一层——官方发布 / Hermes 生成 / 用户引入 / 待确认，按内置清单、hub 安装记录与 curator 账本自动推断，可手工更正。
- **审批中心**：提案先备份，再人工批准，校验 diff 范围与哈希后执行；任何一步失败都能回滚。
- **常用技能**：首页星标列表（上限 12）、备注、排序、一键复制调用提示。
- **定时任务（可选）**：每日盘点、周度四类审计、周报/月报；默认不安装。
- **只读优先**：未经逐项批准，不修改、不删除、不移动任何技能；被扫描内容视为不可信数据，绝不执行其中脚本。

## 前提

- WSL2 + Ubuntu，且已启用 systemd（`/etc/wsl.conf` 中 `systemd=true`）
- 已安装 Hermes Agent，`~/.hermes/skills` 存在
- `git`、`python3`（≥3.10）、`python3-venv`、`python3-yaml`
- 端口 8000 空闲；仅本机访问，不做端口转发

## 安装

```bash
git clone https://github.com/PotatoCatS/hermes-skill-kit.git ~/.hermes/skill-kit
cd ~/.hermes/skill-kit
sudo bash install.sh
```

然后在浏览器打开 <http://localhost:8000>，首次访问设置管理密码（至少 10 个字符）。

安装脚本默认会跑一次盘点；可选参数：

```bash
sudo bash install.sh --no-scan        # 跳过首次盘点
sudo bash install.sh --with-examples  # 复制演示数据，便于先看界面
sudo bash install.sh --port 8011      # 换端口（默认 8000）
bash install.sh --check               # 只读自检，不改任何文件
```

定时任务默认不创建。需要时参考 `pipeline/config/cron-prompts/` 手动配置；它是 Hermes 侧配置，卸载脚本不会自动删除。

更新与卸载：

```bash
cd ~/.hermes/skill-kit
git pull --ff-only                     # 或 sudo bash update.sh --pull
sudo bash update.sh                    # 同步脚本/规则/依赖，重启服务并做健康检查
sudo bash uninstall.sh                 # 停服务，保留数据
sudo bash uninstall.sh --purge         # 连数据一起删除（会二次确认）
```

## 数据放在哪里

| 位置 | 内容 | 是否进 git |
| --- | --- | --- |
| `~/.hermes/skill-kit` | 本仓库（代码、规则、示例） | 是 |
| `~/.hermes/skill-management` | 盘点结果、报告、提案、日志、备份 | 否 |
| `~/.hermes/skill-dashboard-data` | 登录配置、产生方式覆盖、常用列表 | 否 |

看板通过 systemd 单元里的环境变量读取后两个目录，所以代码目录可以随时更新或移动。

## 安全边界

这是一个**单用户、仅回环**的本地工具：没有多用户、没有 TLS，也不打算暴露到局域网或公网。请只在本机浏览器访问 `127.0.0.1:8000`。细节见 [SECURITY.md](docs/SECURITY.md)。

## 文档

- [安装与首次配置](docs/INSTALL.md)
- [日常操作：扫描、审计、提案、回滚](docs/OPERATIONS.md)
- [安全边界与数据卫生](docs/SECURITY.md)
- [管线脚本说明](pipeline/README.md)

## 目录

```
dashboard/        Web 看板（FastAPI + 原生 JS 单页）
pipeline/         扫描、审计、提案、执行、回滚脚本与规则
examples/         脱敏示例数据（仅用于演示）
docs/             安装、操作、安全文档
```

## 许可证

[MIT](LICENSE)
