# 新机部署操作手册

本手册用于在**一台新电脑**上从零部署「Hermes 技能看板 + 技能生命周期管线」。按顺序执行，每一步都有可验证的结果；出错时先停在原地排查，不要跳过检查继续安装。

- 适用系统：Windows 10/11 + WSL2 Ubuntu（原生 Ubuntu 也可，跳过第 1 节）
- 预计耗时：首次 30–60 分钟，其中依赖下载占大半
- 风险边界：安装只读 Hermes 技能，不修改、不移动、不删除任何技能；所有数据写在 `~/.hermes` 下的独立目录

---

## 0. 部署前检查清单

在目标机的 WSL 终端逐条执行，全部符合预期再继续。

| 检查项 | 命令 | 期望 |
| --- | --- | --- |
| WSL 版本 | `wsl.exe --version`（Windows PowerShell） | 输出 WSL 版本，非“未安装” |
| Ubuntu 发行版 | `wsl.exe -l -v` | 有 Ubuntu，版本 2 |
| systemd | `systemctl show-environment` | 有输出，不报 “System has not been booted with systemd” |
| Python | `python3 --version` | ≥ 3.10 |
| venv 与 YAML | `python3 -c 'import venv, yaml; print("ok")'` | 输出 `ok` |
| git | `git --version` | 有版本号 |
| Hermes 技能目录 | `ls -d ~/.hermes/skills` | 目录存在 |
| 技能数量 | `find ~/.hermes/skills -name SKILL.md | wc -l` | 大于 0 |
| 端口 | `ss -ltn | grep ':8000 ' || echo free` | 输出 `free` |
| 网络 | `curl -I -m 10 https://github.com` | 返回 200 / 301 |

任何一项不满足，先看第 10 节对应条目。

> **用户一致性**：后续用 `sudo` 安装时，脚本会自动取 `SUDO_USER` 作为运行用户，并把它当作 Hermes 用户（`/home/<用户>/.hermes`）。如果 Hermes 是 root 跑的（`/root/.hermes`），请以 root 身份直接安装，或显式指定 `HERMES_HOME=/root/.hermes`。**不要**在普通用户 home 和 root home 之间混用。

---

## 1. Windows 侧准备 WSL 与 systemd

如果目标机已有 Ubuntu + systemd，直接跳到第 2 节。

1. 安装 WSL（管理员 PowerShell）：

   ```powershell
   wsl --install -d Ubuntu
   ```

2. 安装完成后在 WSL 里检查 `/etc/wsl.conf`：

   ```bash
   cat /etc/wsl.conf
   ```

   需要有：

   ```ini
   [boot]
   systemd=true
   ```

   没有就用 `sudo nano /etc/wsl.conf` 补上。

3. 回到 Windows PowerShell 重启 WSL 使配置生效：

   ```powershell
   wsl --shutdown
   ```

4. 重新进入 Ubuntu，确认：

   ```bash
   systemctl show-environment && echo SYSTEMD_OK
   ```

> `wsl --shutdown` 会停掉该发行版里所有进程。如果这台机器上已经跑着别的重要服务，选一个空闲时间再做。

---

## 2. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-yaml git curl
```

验证：

```bash
python3 --version
python3 -c 'import venv, yaml; print("deps ok")'
```

`python3-venv` 缺失时，后面的 `python3 -m venv` 会报 `ensurepip is not available`，这是最常见的中断点。

---

## 3. 确认 Hermes 已就绪

本套件**不安装 Hermes**，只管理它已有的技能。目标机必须已经装好 Hermes Agent，且存在：

```bash
ls ~/.hermes/skills
ls ~/.hermes/config.yaml
```

- `~/.hermes/skills`：看板与管线扫描的对象，只需要读权限。
- `~/.hermes/config.yaml`：安装只做存在性检查，**不会修改**。
- 如果同时装了内置技能源（`~/.hermes/hermes-agent/skills`），看板会把它们标记为 `builtin`，同样只读。

记录一下当前技能数量，安装后用来对比：

```bash
find ~/.hermes/skills -name SKILL.md | wc -l
```

---

## 4. 克隆仓库

```bash
git clone https://github.com/PotatoCatS/hermes-skill-kit.git ~/.hermes/skill-kit
cd ~/.hermes/skill-kit
git describe --tags
```

期望输出 `v0.1.1` 或更新的版本号；完全没输出说明拿到的不是 git 仓库（例如 ZIP 解压）。

如果目标机直连 GitHub 失败：

- 有代理时临时带上：`https_proxy=http://<代理地址> git clone ...`；
- 或在 Windows 侧下载 ZIP 解压到 `\\wsl$\Ubuntu\home\<用户>\.hermes\skill-kit`，再在 WSL 里 `cd` 过去。注意 ZIP 解压可能丢失可执行位，执行前先 `chmod +x install.sh update.sh uninstall.sh dashboard/run.sh`。

克隆后确认脚本可执行：

```bash
ls -l install.sh update.sh uninstall.sh
# 期望 -rwxr-xr-x
```

---

## 5. 只读自检（不改任何文件）

```bash
bash install.sh --check
```

脚本会检查：Linux/WSL、systemd、git/python3/venv/PyYAML、仓库结构、模板里是否有硬编码路径、Hermes 技能目录、数据目录、端口占用、已有登录配置与 venv。

输出以汇总行结束：

- `自检通过：0 项失败，N 项警告` → 继续；
- `自检失败：N 项失败` → 按输出的 `[FAIL]` 逐条处理，**不要**强行安装。

常见的“警告”是良性的：数据目录将新建、尚无登录配置、未找到 `config.yaml`。

如果 8000 被占用，安装时换端口：

```bash
bash install.sh --check --port 8011
```

---

## 6. 正式安装

```bash
sudo bash install.sh
```

安装需要 root 只为写 systemd 单元；数据目录与仓库内 venv 的属主会改成运行用户（`SUDO_USER`）。脚本按 13 段执行：

1. 环境自检（第 1–5 段）：系统、依赖、仓库、Hermes、端口；
2. 创建数据目录（`0700`）：`~/.hermes/skill-management`、`~/.hermes/skill-dashboard-data`；
3. 复制 7 个管线脚本；
4. 写入三份规则、六份定时任务提示词、提案模板；
5. 渲染 `manager-config.yaml`（已有则保留）；
6. 创建仓库根目录 `.venv` 并安装 FastAPI/uvicorn；
7. 首次盘点（`registry/skills.json`、`logs/scan-log.md`）；
8. 修正数据目录与 venv 属主为运行用户；
9. 渲染 systemd 单元 `hermes-skill-dashboard.service`；
10. `enable --now` 启动服务；
11. 打印访问地址与日志命令。

常用可选参数：

| 参数 | 用途 |
| --- | --- |
| `--no-scan` | 跳过首次盘点，之后手动跑 |
| `--with-examples` | 复制脱敏示例数据，先看界面 |
| `--port 8011` | 换端口（默认 8000） |
| `--force-config` | 覆盖已有 `manager-config.yaml`（先备份） |
| `--no-systemd` | 只装文件不建服务，用于演练 |

安装结束应看到：

```
[ OK ] 服务已启用: hermes-skill-dashboard
[ OK ] 服务运行中
访问地址: http://localhost:8000
```

---

## 7. 首次访问与设置密码

在 Windows 浏览器打开 <http://localhost:8000>。

- 只在本机访问：服务只绑定 WSL 内的 `127.0.0.1`，不要用 WSL 的 IP，也不要改成 `0.0.0.0`；
- 首次进入要求设置管理密码，**至少 10 个字符**；
- 密码只保存 PBKDF2-SHA256 哈希，登录态 12 小时，Cookie 为 `HttpOnly` + `SameSite=Strict`；
- 凭据文件在 `~/.hermes/skill-dashboard-data/config/`（`0600`），不要复制到仓库或聊天里。

如果 Windows 打不开但 WSL 内 `curl` 正常，检查 Windows 用户目录下 `.wslconfig` 是否关闭了 `localhostForwarding`（默认开启）。

---

## 8. 安装后验收

逐条执行，四项都通过才算部署完成：

```bash
systemctl status hermes-skill-dashboard --no-pager
curl -s http://127.0.0.1:8000/api/health
journalctl -u hermes-skill-dashboard -n 50 --no-pager
python3 -c "import json;d=json.load(open('$HOME/.hermes/skill-management/registry/skills.json'));print('skills:',len(d.get('skills',[])))"
```

期望：

| 检查 | 期望 |
| --- | --- |
| 服务状态 | `active (running)`，且 `enabled` |
| 健康检查 | `{"status":"ok", ...}` |
| 日志 | 无 traceback；只有启动与访问日志 |
| 盘点结果 | 技能条数与第 3 节记录一致（示例数据模式除外） |

再打开看板，确认：

- 列表能显示技能，来源列有 `builtin` / `local`；
- 「质量 / 安全 / 重复 / 更新」筛选与统计正常；
- 点开任一技能能看到 `SKILL.md` 渲染内容；
- 首页「常用技能」可以星标、写备注、复制调用提示。

---

## 9. 日常使用与维护

### 9.1 看板能做什么

- 盘点：查看技能、分类、版本、哈希、来源与加载状态；
- 审计：查看四类报告与证据；
- 提案：在提案页批准/拒绝，执行与回滚走同一套管线；
- 记录：批准、拒绝、执行、回滚都会写入操作审计。

**对技能文件的写操作只走提案流程**；星标、备注、产生方式覆盖等只写看板自己的状态目录，不碰技能。

### 9.2 常用命令

```bash
systemctl status hermes-skill-dashboard --no-pager
journalctl -u hermes-skill-dashboard -n 50 --no-pager
tail -f ~/.hermes/skill-management/logs/scan-log.md
```

### 9.3 数据在哪

| 路径 | 内容 |
| --- | --- |
| `~/.hermes/skill-kit` | 代码与文档（可随时更新） |
| `~/.hermes/skill-management` | 盘点、报告、审计、提案、日志、备份 |
| `~/.hermes/skill-dashboard-data` | 登录配置、常用列表、产生方式覆盖 |
| `/var/backups/hermes-skill-kit` | 更新前的 systemd 单元副本 |

### 9.4 升级

```bash
cd ~/.hermes/skill-kit
sudo bash update.sh --pull
```

`update.sh` 会先 `git pull --ff-only`（工作区有改动会拒绝），再同步脚本、规则、依赖，重启并做健康检查。更新不会覆盖 `manager-config.yaml` 和已有规则。

如果健康检查失败，脚本会打印回退命令。手动回退：

```bash
cd ~/.hermes/skill-kit
git log --oneline -5
git checkout <上一个提交或标签>
sudo bash update.sh --no-scan
```

升级前建议备份两个数据目录（见 9.6），改配置时才用 `--force-config`。

### 9.5 修改配置

```bash
sudo nano ~/.hermes/skill-management/config/manager-config.yaml
sudo systemctl restart hermes-skill-dashboard
```

`manager-config.yaml` 里的路径在安装时已按目标机渲染，通常不需要改。改完要重启服务。

### 9.6 备份与迁移

复制两个目录即可迁移全部状态：

```bash
tar -czf skm-backup-$(date +%Y%m%d).tgz \
  -C ~/.hermes skill-management skill-dashboard-data
```

恢复时解压回 `~/.hermes/`，保持目录权限 `0700`，再重启服务。备份包含登录配置与会话密钥，按凭据对待。

---

## 10. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `System has not been booted with systemd` | `/etc/wsl.conf` 未开 `systemd=true`，或改完没执行 `wsl --shutdown` |
| `ensurepip is not available` | 缺 `python3-venv`，`sudo apt install python3-venv` 后重跑 |
| `ModuleNotFoundError: yaml` | 缺 `python3-yaml`，安装后重跑 `--check` |
| 8000 被占用 | `ss -ltnp 'sport = :8000'` 找到进程；或 `--port 8011` 重装 |
| 浏览器打不开 | 确认 `curl 127.0.0.1:8000/api/health` 在 WSL 内正常；检查 `.wslconfig` 的 localhostForwarding |
| 页面空白/样式旧 | Ctrl+F5 强制刷新；确认 `journalctl` 无 500 |
| 盘点结果为 0 | `~/.hermes/skills` 下没有 `SKILL.md`，或 `manager-config.yaml` 的 `skills_dir` 不对 |
| 服务起不来 | `journalctl -u hermes-skill-dashboard -n 100`，多为 venv 依赖或路径权限 |
| 忘记密码 | `sudo systemctl stop hermes-skill-dashboard`，删除 `~/.hermes/skill-dashboard-data/config/admin.json`（**不要删** `session.key`），重启后重新设置 |
| 更新后行为异常 | 按 9.4 回退到上一个提交，再 `sudo bash update.sh --no-scan` |
| GitHub 克隆失败 | 配代理，或用 ZIP + `chmod +x` 方式 |

---

## 11. 可选：定时任务

安装**不会**自动创建定时任务。套件提供六份提示词（`pipeline/config/cron-prompts/skm-02.txt` … `skm-07.txt`），分别对应质量审计、安全审计、更新检查、重复分析、月报、周报。

在 Hermes 侧创建任务时注意：

1. 工作目录指向 `~/.hermes/skill-management`，提示词里的 `registry/`、`audits/`、`reports/` 都是相对该目录的路径；
2. 任务使用 `--deliver local`，结果只落本地，不外发；
3. 建议错开时间，避免多个审计同时写 `registry/`；
4. 这些任务依赖 `skill-lifecycle-manager` 技能与 `skm_daily_scan.py` / `skm_context.py` 等辅助脚本——它们**不在本仓库内**，需要从源机器另行复制并确认路径；
5. 查看与清理：

   ```bash
   hermes cron list
   hermes cron runs <name>
   hermes cron remove <name>
   ```

没有把握时先不建 cron，手动在看板里跑审计不影响主体功能。

---

## 12. 卸载

```bash
cd ~/.hermes/skill-kit
sudo bash uninstall.sh            # 停服务、删单元、删仓库内 .venv，保留数据
sudo bash uninstall.sh --purge    # 连数据一起删除，需输入 PURGE 确认
```

卸载不会删除仓库目录，也不会动 Hermes 全局配置和 cron 任务。要彻底清干净，再手动：

```bash
rm -rf ~/.hermes/skill-kit
hermes cron list        # 如有相关任务，逐个 remove
```

---

## 13. 部署完成检查表

- [ ] `systemctl is-active hermes-skill-dashboard` 输出 `active`
- [ ] `systemctl is-enabled hermes-skill-dashboard` 输出 `enabled`
- [ ] `/api/health` 返回 `status: ok`
- [ ] 浏览器能打开、密码已设置
- [ ] 技能条数与安装前一致
- [ ] `git describe --tags` 与预期版本一致
- [ ] 备份策略已确认（两个数据目录）
- [ ] 如启用 cron，已确认辅助技能与脚本就位

全部勾选后，在项目笔记里记录：部署日期、目标机名、仓库版本、端口、Hermes 用户。以后升级或排障都以此为准。
