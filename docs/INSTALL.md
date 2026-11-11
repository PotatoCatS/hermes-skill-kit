# 安装

## 1. 前置检查

在 WSL 终端执行：

```bash
cat /etc/wsl.conf                 # 需要 systemd=true（没有就加上，然后 wsl --shutdown 重启）
systemctl --version               # 能输出版本号说明 systemd 可用
git --version
python3 --version                 # ≥ 3.10
python3 -c 'import venv, yaml'    # 缺 yaml 时：sudo apt install python3-yaml
ls -d ~/.hermes/skills            # 必须存在
ss -ltn | grep ':8000 ' || echo "8000 空闲"
```

如果 `systemctl` 报 “System has not been booted with systemd”，先修改 `/etc/wsl.conf`：

```ini
[boot]
systemd=true
```

保存后回到 Windows 执行 `wsl --shutdown`，再重新进入 WSL。

## 2. 克隆与安装

```bash
git clone https://github.com/PotatoCatS/hermes-skill-kit.git ~/.hermes/skill-kit
cd ~/.hermes/skill-kit
sudo bash install.sh
```

`install.sh` 会做这些事：

1. 检查 systemd、git、python3-venv、python3-yaml、8000 端口与 `~/.hermes/skills`；
2. 创建数据目录（`0700`）：`~/.hermes/skill-management`、`~/.hermes/skill-dashboard-data`，属主改为运行用户；
3. 复制管线配置（已存在则跳过，`--force-config` 才覆盖）；
4. 创建仓库根目录的 `.venv` 并安装 `dashboard/requirements.txt`；
5. 渲染 systemd 单元（用户名、Home、仓库路径、端口），`daemon-reload` 后 `enable --now`；
6. 默认执行一次盘点（`--no-scan` 跳过）；
7. 打印访问地址、日志命令与首次设置密码提示。

完整的按步骤部署流程（含 Windows/WSL 准备、验收与排障）见 [部署操作手册](DEPLOYMENT.md)。

### 可选参数

| 参数 | 作用 |
| --- | --- |
| `--check` | 只做只读自检：系统、依赖、仓库结构、模板、Hermes 路径、端口；不改任何文件 |
| `--no-scan` | 不在安装末尾跑首次盘点 |
| `--with-examples` | 把 `examples/` 复制到数据目录，没有技能时也能先看界面 |
| `--force-config` | 覆盖已存在的 `manager-config.yaml`（会先备份旧文件） |
| `--port N` | 看板端口，默认 8000 |
| `--no-systemd` | 不安装 systemd 单元（演练用，需自行启动服务） |

首次安装前建议先跑一次自检：

```bash
bash install.sh --check
```

## 3. 首次访问

浏览器打开 <http://localhost:8000>。首次访问会要求设置管理密码：

- 至少 10 个字符；只保存 PBKDF2-SHA256 哈希；
- 密码与会话密钥写在 `~/.hermes/skill-dashboard-data/config/`（`0600`）；
- 会话有效期 12 小时，Cookie 为 `HttpOnly` + `SameSite=Strict`。

**忘记密码**：停止服务，删除 `config/admin.json`（不要删除 `session.key`），重启后重新设置。注意这会把已有会话全部作废。

## 4. 验证

```bash
systemctl status hermes-skill-dashboard --no-pager
curl -sS http://127.0.0.1:8000/api/health
journalctl -u hermes-skill-dashboard -n 50 --no-pager
```

预期：服务为 `active (running)`，健康检查返回 `{"status": "ok", ...}`，日志里没有 traceback。

## 5. 更新

```bash
cd ~/.hermes/skill-kit
sudo bash update.sh --pull     # 先 git pull --ff-only，再同步
```

`update.sh` 会：

1. 从现有 systemd 单元读回用户、端口、venv 与数据目录（不会把 `sudo` 更新改回默认值）；
2. 把 `pipeline/scripts/*.py` 覆盖到数据目录，覆盖前打包备份到 `backups/scripts-<时间>.tar.gz`；
3. 补写缺失的规则文件，刷新 cron 提示词与提案模板；
4. 保留 `manager-config.yaml`（`--force-config` 才覆盖）；
5. 同步 venv 依赖，默认跑一次盘点（`--no-scan` 跳过）；
6. 重新渲染 systemd 单元、重启服务，并请求 `/api/health` 验证。

参数：`--pull`、`--no-scan`、`--force-config`、`--port N`、`--no-restart`、`--check`（只读检查安装状态）。

健康检查失败时会打印回退方式：`git log --oneline -5` 找到上一个提交，`git checkout <commit>` 后重新运行 `update.sh`。

## 6. 卸载

```bash
sudo bash uninstall.sh           # 停服务、删单元、删仓库内 .venv，保留全部数据
sudo bash uninstall.sh --purge   # 连数据目录一起删除（需输入 PURGE 确认，或加 --yes）
```

默认保留 `~/.hermes/skill-management` 与 `~/.hermes/skill-dashboard-data`，打印路径供你决定。`--purge` 前会校验两个目录都在 `~/.hermes` 之下，拒绝删除 `/` 或家目录。`--keep-venv` 保留仓库内环境，`--no-systemd` 仅用于演练。

卸载不会删除仓库目录本身，也不碰 Hermes 全局配置和 cron 任务；定时任务用 `hermes cron list` 查看、`hermes cron remove` 删除。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `System has not been booted with systemd` | 按第 1 节修改 `/etc/wsl.conf` 并 `wsl --shutdown` |
| 服务起不来 | `journalctl -u hermes-skill-dashboard -n 50`，多为 venv 依赖或端口占用 |
| 浏览器打不开 | 确认服务监听 `127.0.0.1:8000`；不要用 WSL 的 IP 访问 |
| 页面是旧样式 | 强制刷新（Ctrl+F5）；静态资源带版本号，正常更新会自动失效缓存 |
| 盘点结果为 0 | 检查 `~/.hermes/skills` 是否有 `SKILL.md`，以及 `manager-config.yaml` 的 `skills_dir` |
