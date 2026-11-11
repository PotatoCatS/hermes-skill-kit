#!/usr/bin/env bash
# hermes-skill-kit installer
#
# 用法:
#   sudo bash install.sh [--no-scan] [--with-examples] [--force-config] [--port N]
#   bash install.sh --check [--no-systemd] [--port N]
#
# --check 只做只读自检，不创建目录、不装依赖、不写 systemd。
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$REPO_DIR/dashboard"
PIPELINE_DIR="$REPO_DIR/pipeline"
VENV_DIR="${SKD_VENV:-$REPO_DIR/.venv}"
SERVICE_NAME="${SKD_SERVICE_NAME:-hermes-skill-dashboard}"

DO_CHECK=0
DO_SCAN=1
WITH_EXAMPLES=0
FORCE_CONFIG=0
WITH_SYSTEMD=1
PORT="${SKD_PORT:-8000}"

usage() {
  cat <<'EOF'
用法: sudo bash install.sh [选项]

选项:
  --no-scan         不在安装末尾跑首次盘点
  --with-examples   把 examples/ 复制到数据目录（仅在 registry 不存在时）
  --force-config    覆盖已有的 manager-config.yaml（先备份）
  --port N          看板端口，默认 8000
  --no-systemd      不安装 systemd 单元（演练用，需要自行启动服务）
  --check           只做只读自检，不改任何文件
  -h, --help        显示本帮助

可用环境变量:
  HERMES_HOME       Hermes 根目录，默认 <用户 home>/.hermes
  SKD_BASE_DIR      管线数据目录，默认 $HERMES_HOME/skill-management
  SKD_STATE_DIR     看板状态目录，默认 $HERMES_HOME/skill-dashboard-data
  SKD_PORT          端口，等价于 --port
  SKD_VENV          venv 路径，默认 <仓库>/.venv（演练时可复用已有环境）
EOF
}

while (($#)); do
  case "$1" in
    --check) DO_CHECK=1 ;;
    --no-scan) DO_SCAN=0 ;;
    --with-examples) WITH_EXAMPLES=1 ;;
    --force-config) FORCE_CONFIG=1 ;;
    --no-systemd) WITH_SYSTEMD=0 ;;
    --port)
      [[ $# -ge 2 ]] || { echo "缺少 --port 的值" >&2; exit 2; }
      PORT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

FAILS=0
WARNS=0
ok()   { printf '  [ OK ] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; WARNS=$((WARNS + 1)); }
fail() { printf '  [FAIL] %s\n' "$1"; FAILS=$((FAILS + 1)); }
step() { printf '\n== %s ==\n' "$1"; }

if [[ $EUID -eq 0 ]]; then
  RUN_USER="${SUDO_USER:-root}"
else
  RUN_USER="$(id -un)"
fi
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
[[ -n "${RUN_HOME:-}" ]] || RUN_HOME="${HOME:-/root}"

HERMES_HOME="${HERMES_HOME:-$RUN_HOME/.hermes}"
BASE_DIR="${SKD_BASE_DIR:-$HERMES_HOME/skill-management}"
STATE_DIR="${SKD_STATE_DIR:-$HERMES_HOME/skill-dashboard-data}"
CONFIG_DIR="${SKD_CONFIG_DIR:-$STATE_DIR/config}"
DATA_DIR="${SKD_DATA_DIR:-$STATE_DIR/data}"

port_busy() {
  python3 - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

check() {
  step "1. 系统环境"
  if [[ "$(uname -s)" == "Linux" ]]; then
    ok "Linux / $(uname -r)"
  else
    fail "非 Linux；本项目面向 WSL2 Ubuntu"
  fi
  if [[ -f /proc/version ]] && grep -qi microsoft /proc/version; then
    ok "WSL 环境"
  else
    warn "未检测到 WSL；普通 Ubuntu 也可以运行"
  fi

  if command -v systemctl >/dev/null 2>&1 && systemctl show-environment >/dev/null 2>&1; then
    ok "systemd 可用"
  else
    if (( WITH_SYSTEMD )); then
      fail "systemd 不可用；请在 /etc/wsl.conf 设置 [boot] systemd=true 后 wsl --shutdown"
    else
      warn "systemd 不可用（已指定 --no-systemd）"
    fi
  fi

  step "2. 依赖"
  local cmd
  for cmd in git python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
      ok "$cmd: $(command -v "$cmd")"
    else
      fail "缺少 $cmd"
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PY' >/dev/null 2>&1
import sys, venv, yaml  # noqa: F401
assert sys.version_info >= (3, 10), sys.version_info
PY
    then
      ok "python3 $(python3 -c 'import platform; print(platform.python_version())')，venv 与 PyYAML 可用"
    else
      fail "python3 需要 ≥3.10，且 venv / PyYAML 可导入（sudo apt install python3-venv python3-yaml）"
    fi
  fi

  step "3. 仓库结构与模板"
  local f
  for f in \
    dashboard/app/main.py \
    dashboard/requirements.txt \
    dashboard/deploy/hermes-skill-dashboard.service.in \
    pipeline/scripts/scan_skills.py \
    pipeline/config/manager-config.yaml.in \
    pipeline/proposals/TEMPLATE.yaml
  do
    if [[ -f "$REPO_DIR/$f" ]]; then
      ok "$f"
    else
      fail "缺少 $f"
    fi
  done
  if grep -q '/root/' "$REPO_DIR/dashboard/deploy/hermes-skill-dashboard.service.in" \
     "$REPO_DIR/pipeline/config/manager-config.yaml.in" 2>/dev/null; then
    fail "模板里仍有 /root/ 绝对路径"
  else
    ok "模板未包含 /root/ 绝对路径"
  fi
  if grep -q '@REPO@' "$REPO_DIR/dashboard/deploy/hermes-skill-dashboard.service.in"; then
    ok "service 单元含 @REPO@ 占位"
  else
    fail "service 单元缺少 @REPO@ 占位"
  fi

  step "4. Hermes 与数据位置"
  if [[ -d "$HERMES_HOME/skills" ]]; then
    ok "技能目录: $HERMES_HOME/skills ($(find "$HERMES_HOME/skills" -name SKILL.md 2>/dev/null | wc -l) 个 SKILL.md)"
  else
    fail "找不到 $HERMES_HOME/skills；请先安装 Hermes Agent"
  fi
  if [[ -f "$HERMES_HOME/config.yaml" ]]; then
    ok "Hermes 配置: $HERMES_HOME/config.yaml（只读）"
  else
    warn "未找到 $HERMES_HOME/config.yaml"
  fi
  [[ -d "$BASE_DIR" ]] && ok "管线数据目录已存在: $BASE_DIR" \
                        || warn "管线数据目录将新建: $BASE_DIR"
  [[ -d "$STATE_DIR" ]] && ok "看板状态目录已存在: $STATE_DIR" \
                         || warn "看板状态目录将新建: $STATE_DIR"

  step "5. 端口与冲突"
  if command -v python3 >/dev/null 2>&1; then
    if port_busy "$PORT"; then
      ok "127.0.0.1:$PORT 可用"
    else
      fail "127.0.0.1:$PORT 已被占用；换 --port 或停止占用进程"
    fi
  fi
  if [[ -f "$CONFIG_DIR/admin.json" ]]; then
    warn "已存在登录配置 $CONFIG_DIR/admin.json；安装不会覆盖，卸载 --purge 才会删除"
  else
    ok "尚无登录配置，首次访问将设置密码"
  fi
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    warn "已存在 venv: $VENV_DIR（安装会复用）"
  else
    ok "将新建 venv: $VENV_DIR"
  fi
}

if (( DO_CHECK )); then
  check
  printf '\n----------------------------------------\n'
  if (( FAILS > 0 )); then
    printf '自检失败：%d 项失败，%d 项警告\n' "$FAILS" "$WARNS"
    exit 1
  fi
  printf '自检通过：0 项失败，%d 项警告\n' "$WARNS"
  printf '提示：这是只读自检，没有创建目录、没有安装依赖、没有写 systemd。\n'
  exit 0
fi

die() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }
note() { printf '[ .. ] %s\n' "$1"; }

if (( WITH_SYSTEMD )) && [[ $EUID -ne 0 ]]; then
  die "安装 systemd 单元需要 root：请用 sudo bash install.sh（演练可加 --no-systemd）"
fi

check
if (( FAILS > 0 )); then
  die "自检未通过，已中止安装；先处理上面的 FAIL 项"
fi

step "6. 创建数据目录"
install -d -m 700 "$BASE_DIR" "$BASE_DIR/scripts" "$BASE_DIR/config" "$BASE_DIR/config/cron-prompts" \
  "$BASE_DIR/registry" "$BASE_DIR/reports" "$BASE_DIR/reports/daily" "$BASE_DIR/reports/weekly" \
  "$BASE_DIR/reports/monthly" "$BASE_DIR/audits" "$BASE_DIR/audits/quality" "$BASE_DIR/audits/security" \
  "$BASE_DIR/audits/duplication" "$BASE_DIR/audits/updates" "$BASE_DIR/proposals" \
  "$BASE_DIR/proposals/pending" "$BASE_DIR/proposals/approved" "$BASE_DIR/proposals/rejected" \
  "$BASE_DIR/logs" "$BASE_DIR/backups" "$STATE_DIR" "$CONFIG_DIR" "$DATA_DIR"
ok "数据目录就绪（0700）"

step "7. 同步管线脚本与规则"
cp -f "$PIPELINE_DIR"/scripts/*.py "$BASE_DIR/scripts/"
chmod 755 "$BASE_DIR"/scripts/*.py
ok "脚本已同步到 $BASE_DIR/scripts"

for f in audit-rules.md category-rules.md security-rules.md; do
  if [[ -f "$BASE_DIR/config/$f" ]]; then
    ok "保留已有规则: config/$f"
  else
    cp -f "$PIPELINE_DIR/config/$f" "$BASE_DIR/config/$f"
    ok "写入规则: config/$f"
  fi
done
cp -f "$PIPELINE_DIR"/config/cron-prompts/*.txt "$BASE_DIR/config/cron-prompts/"
cp -f "$PIPELINE_DIR/proposals/TEMPLATE.yaml" "$BASE_DIR/proposals/TEMPLATE.yaml"
ok "定时任务提示词与提案模板已就绪"

render_template() {
  local tpl="$1" out="$2"
  python3 - "$tpl" "$out" "$RUN_USER" "$REPO_DIR" "$VENV_DIR" "$HERMES_HOME" "$BASE_DIR" "$CONFIG_DIR" "$DATA_DIR" "$PORT" <<'PY'
import sys
from pathlib import Path

tpl, out, user, repo, venv, hermes, base, cfg, data, port = sys.argv[1:11]
values = {
    "@USER@": user,
    "@OWNER@": user,
    "@REPO@": repo,
    "@VENV@": venv,
    "@HERMES_HOME@": hermes,
    "@BASE_DIR@": base,
    "@CONFIG_DIR@": cfg,
    "@DATA_DIR@": data,
    "@PORT@": port,
    "@SKILLS_DIR@": f"{hermes}/skills",
    "@BUNDLED_SKILLS_DIR@": f"{hermes}/hermes-agent/skills",
    "@CONFIG_PATH@": f"{hermes}/config.yaml",
    "@LOADER_SOURCE@": f"{hermes}/hermes-agent/hermes_constants.py",
}
text = Path(tpl).read_text(encoding="utf-8")
for key, value in values.items():
    text = text.replace(key, value)
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text(text, encoding="utf-8")
PY
}

step "8. 渲染管理配置"
CFG="$BASE_DIR/config/manager-config.yaml"
if [[ -f "$CFG" && $FORCE_CONFIG -eq 0 ]]; then
  ok "保留已有 manager-config.yaml（--force-config 才覆盖）"
else
  if [[ -f "$CFG" ]]; then
    cp -a "$CFG" "$CFG.bak-$(date +%Y%m%d%H%M%S)"
    note "已备份旧配置"
  fi
  render_template "$PIPELINE_DIR/config/manager-config.yaml.in" "$CFG"
  chmod 600 "$CFG"
  ok "已生成 $CFG"
fi

step "9. Python 环境"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "已创建 venv: $VENV_DIR"
else
  ok "复用已有 venv: $VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q -r "$DASHBOARD_DIR/requirements.txt"
ok "依赖安装完成"

step "10. 首次盘点"
if (( DO_SCAN )); then
  if HERMES_HOME="$HERMES_HOME" "$VENV_DIR/bin/python" "$BASE_DIR/scripts/scan_skills.py" \
      --out-dir "$BASE_DIR/registry" --scan-log "$BASE_DIR/logs/scan-log.md"; then
    ok "盘点完成: $BASE_DIR/registry/skills.json"
  else
    warn "首次盘点失败；服务仍会启动，可稍后手动重跑"
  fi
else
  note "已指定 --no-scan，跳过首次盘点"
fi

if (( WITH_EXAMPLES )); then
  step "11. 示例数据"
  if [[ ! -f "$BASE_DIR/registry/skills.json" ]]; then
    cp -f "$REPO_DIR/examples/registry/skills.example.json" "$BASE_DIR/registry/skills.json"
    ok "已复制示例 registry"
  else
    warn "registry 已存在，未覆盖示例数据"
  fi
  cp -f "$REPO_DIR/examples/audits/summary-example.json" "$BASE_DIR/audits/summary-example.json"
  cp -f "$REPO_DIR/examples/reports/weekly-example.md" "$BASE_DIR/reports/weekly/weekly-example.md"
  ok "示例审计摘要与周报已复制"
fi

step "12. 修正属主"
if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]]; then
  chown -R "$RUN_USER":"$RUN_USER" "$BASE_DIR" "$STATE_DIR"
  case "$VENV_DIR" in
    "$REPO_DIR"/*) chown -R "$RUN_USER":"$RUN_USER" "$VENV_DIR" ;;
  esac
  ok "数据目录与仓库内 venv 属主: $RUN_USER"
else
  note "运行用户为 $RUN_USER，无需调整属主"
fi

if (( WITH_SYSTEMD )); then
  step "13. systemd 单元"
  UNIT="/etc/systemd/system/$SERVICE_NAME.service"
  render_template "$DASHBOARD_DIR/deploy/hermes-skill-dashboard.service.in" "$UNIT"
  chmod 644 "$UNIT"
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"
  ok "服务已启用: $SERVICE_NAME"
  sleep 1
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "服务运行中"
  else
    warn "服务未处于 active；查看: journalctl -u $SERVICE_NAME -n 50"
  fi
else
  step "13. systemd 单元（已跳过）"
  note "未安装服务；可手动运行: cd $REPO_DIR/dashboard && PORT=$PORT ./run.sh"
fi

step "完成"
cat <<EOF
访问地址: http://localhost:$PORT
首次访问会要求设置管理密码（至少 10 个字符）。

数据目录:
  管线: $BASE_DIR
  看板: $STATE_DIR

常用命令:
  systemctl status $SERVICE_NAME --no-pager
  journalctl -u $SERVICE_NAME -n 50 --no-pager
  tail -f $BASE_DIR/logs/scan-log.md
EOF
