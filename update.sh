#!/usr/bin/env bash
# hermes-skill-kit updater
#
# 用法:
#   sudo bash update.sh [--pull] [--no-scan] [--force-config] [--port N] [--no-restart]
#   bash update.sh --check
#
# 只更新程序与通用配置：管线脚本、cron 提示词、提案模板、看板依赖与 systemd 单元。
# 不会覆盖 manager-config.yaml（除非 --force-config）和已有规则文件，也不会动数据。
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$REPO_DIR/dashboard"
PIPELINE_DIR="$REPO_DIR/pipeline"
SERVICE_NAME="${SKD_SERVICE_NAME:-hermes-skill-dashboard}"
UNIT="${SKD_UNIT:-/etc/systemd/system/$SERVICE_NAME.service}"
BACKUP_DIR="${SKD_BACKUP_DIR:-/var/backups/hermes-skill-kit}"

DO_CHECK=0
DO_PULL=0
DO_SCAN=1
FORCE_CONFIG=0
DO_RESTART=1
PORT="${SKD_PORT:-}"
VENV_DIR="${SKD_VENV:-}"

usage() {
  cat <<'EOF'
用法: sudo bash update.sh [选项]

选项:
  --pull            先执行 git pull --ff-only（工作区必须干净）
  --no-scan         更新后不跑盘点
  --force-config    覆盖已有的 manager-config.yaml（先备份）
  --port N          端口；默认沿用现有 systemd 单元，否则 8000
  --no-restart      不重启服务（演练或手动重启时用）
  --check           只做只读检查，不改任何文件
  -h, --help        显示本帮助

可用环境变量:
  HERMES_HOME       Hermes 根目录，默认沿用现有单元，否则 <用户 home>/.hermes
  SKD_BASE_DIR      管线数据目录，默认 $HERMES_HOME/skill-management
  SKD_STATE_DIR     看板状态目录，默认 $HERMES_HOME/skill-dashboard-data
  SKD_PORT          端口，等价于 --port
  SKD_VENV          venv 路径，默认沿用现有单元，否则 <仓库>/.venv
  SKD_UNIT          systemd 单元路径，默认 /etc/systemd/system/hermes-skill-dashboard.service（演练用）
  SKD_BACKUP_DIR    单元备份目录，默认 /var/backups/hermes-skill-kit
EOF
}

while (($#)); do
  case "$1" in
    --check) DO_CHECK=1 ;;
    --pull) DO_PULL=1 ;;
    --no-scan) DO_SCAN=0 ;;
    --force-config) FORCE_CONFIG=1 ;;
    --no-restart) DO_RESTART=0 ;;
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
die()  { printf '[FAIL] %s\n' "$1" >&2; exit 1; }
note() { printf '[ .. ] %s\n' "$1"; }

if [[ $EUID -eq 0 ]]; then
  RUN_USER="${SUDO_USER:-root}"
else
  RUN_USER="$(id -un)"
fi
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
[[ -n "${RUN_HOME:-}" ]] || RUN_HOME="${HOME:-/root}"

# 优先沿用现有单元里的值，避免 sudo 更新时把用户、端口、路径改回默认
UNIT_USER=""
UNIT_PORT=""
UNIT_VENV=""
UNIT_HERMES_HOME=""
UNIT_BASE_DIR=""
UNIT_STATE_DIR=""
UNIT_CONFIG_DIR=""
UNIT_DATA_DIR=""
if [[ -f "$UNIT" ]]; then
  UNIT_USER="$(sed -n 's/^User=//p' "$UNIT" | head -n1)"
  UNIT_PORT="$(sed -n 's/.*--port \([0-9]\{1,5\}\).*/\1/p' "$UNIT" | head -n1)"
  UNIT_VENV="$(sed -n 's|^ExecStart=\(.*\)/bin/uvicorn.*|\1|p' "$UNIT" | head -n1)"
  UNIT_HERMES_HOME="$(sed -n 's/^Environment=SKD_HERMES_HOME=//p' "$UNIT" | head -n1)"
  UNIT_BASE_DIR="$(sed -n 's/^Environment=SKD_BASE_DIR=//p' "$UNIT" | head -n1)"
  UNIT_STATE_DIR="$(sed -n 's/^Environment=SKD_STATE_DIR=//p' "$UNIT" | head -n1)"
  UNIT_CONFIG_DIR="$(sed -n 's/^Environment=SKD_CONFIG_DIR=//p' "$UNIT" | head -n1)"
  UNIT_DATA_DIR="$(sed -n 's/^Environment=SKD_DATA_DIR=//p' "$UNIT" | head -n1)"
fi

[[ -n "${UNIT_USER:-}" ]] && RUN_USER="$UNIT_USER"
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
[[ -n "${RUN_HOME:-}" ]] || RUN_HOME="${HOME:-/root}"

HERMES_HOME="${HERMES_HOME:-${UNIT_HERMES_HOME:-$RUN_HOME/.hermes}}"
BASE_DIR="${SKD_BASE_DIR:-${UNIT_BASE_DIR:-$HERMES_HOME/skill-management}}"
STATE_DIR="${SKD_STATE_DIR:-${UNIT_STATE_DIR:-$HERMES_HOME/skill-dashboard-data}}"
CONFIG_DIR="${SKD_CONFIG_DIR:-${UNIT_CONFIG_DIR:-$STATE_DIR/config}}"
DATA_DIR="${SKD_DATA_DIR:-${UNIT_DATA_DIR:-$STATE_DIR/data}}"
PORT="${PORT:-${UNIT_PORT:-8000}}"
VENV_DIR="${VENV_DIR:-${UNIT_VENV:-$REPO_DIR/.venv}}"

health() {
  python3 - "$1" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    print(body)
    sys.exit(0 if body.get("status") == "ok" or body.get("ok") is True else 1)
except Exception as exc:  # noqa: BLE001
    print(f"health check failed: {exc}")
    sys.exit(1)
PY
}

wait_health() {
  local i
  for i in $(seq 1 15); do
    if health "$1" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

check() {
  local installed=1
  step "1. 仓库"
  if [[ -f "$REPO_DIR/dashboard/app/main.py" && -f "$REPO_DIR/pipeline/scripts/scan_skills.py" ]]; then
    ok "仓库结构完整: $REPO_DIR"
  else
    fail "仓库不完整；请在 clone 出来的仓库根目录运行"
  fi
  if [[ -d "$REPO_DIR/.git" ]]; then
    ok "git 工作区: $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
    if [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null || true)" ]]; then
      warn "工作区有未提交改动；--pull 会被拒绝"
    fi
  else
    warn "不是 git 工作区；--pull 不可用"
  fi

  step "2. 安装状态"
  if [[ -d "$BASE_DIR/scripts" ]]; then
    ok "管线目录: $BASE_DIR"
  else
    fail "未检测到安装（$BASE_DIR/scripts 不存在）；先运行 install.sh"
    installed=0
  fi
  if [[ -d "$STATE_DIR" ]]; then
    ok "看板状态目录: $STATE_DIR"
  else
    warn "看板状态目录不存在: $STATE_DIR"
  fi
  if [[ -f "$BASE_DIR/config/manager-config.yaml" ]]; then
    ok "管理配置存在，将保留"
  else
    warn "缺少 manager-config.yaml，更新会重新渲染"
  fi
  if [[ -f "$CONFIG_DIR/admin.json" ]]; then
    ok "登录配置存在（不覆盖）"
  else
    warn "尚无登录配置；首次访问会要求设置密码"
  fi
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    ok "venv: $VENV_DIR"
  else
    warn "venv 不存在，将新建: $VENV_DIR"
  fi

  step "3. systemd 与端口"
  if [[ -f "$UNIT" ]]; then
    ok "单元: $UNIT"
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      ok "服务运行中（端口 $PORT）"
    else
      warn "服务未运行"
    fi
  else
    if (( DO_RESTART )); then
      warn "未找到 $UNIT；更新后不会重启服务"
    else
      ok "无 systemd 单元（已指定 --no-restart）"
    fi
  fi

  step "4. 依赖"
  if python3 - <<'PY' >/dev/null 2>&1
import sys, venv
assert sys.version_info >= (3, 10), sys.version_info
PY
  then
    ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
  else
    fail "python3 需要 ≥3.10 且 venv 可用"
  fi

  (( installed )) || return 0
}

if (( DO_CHECK )); then
  check
  printf '\n----------------------------------------\n'
  if (( FAILS > 0 )); then
    printf '检查失败：%d 项失败，%d 项警告\n' "$FAILS" "$WARNS"
    exit 1
  fi
  printf '检查通过：0 项失败，%d 项警告\n' "$WARNS"
  printf '提示：只读检查，没有改动任何文件。\n'
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  if [[ -f "$UNIT" ]]; then
    die "写入 $UNIT 需要 root：请用 sudo bash update.sh（演练可设 SKD_UNIT 指向可写路径）"
  fi
fi

check
(( FAILS == 0 )) || die "检查未通过，已中止；先处理上面的 FAIL 项"

if (( DO_PULL )); then
  step "5. 拉取代码"
  [[ -d "$REPO_DIR/.git" ]] || die "不是 git 工作区，无法 --pull"
  if [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]; then
    die "工作区有未提交改动；先提交或 stash 再 --pull"
  fi
  before="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
  git -C "$REPO_DIR" pull --ff-only
  after="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
  if [[ "$before" == "$after" ]]; then
    ok "已是最新（$after）"
  else
    ok "代码已更新：$before → $after"
  fi
else
  note "未指定 --pull，只同步本地仓库中的文件"
fi

step "6. 同步管线脚本"
mkdir -p "$BASE_DIR/backups" "$BASE_DIR/scripts"
BACKUP="$BASE_DIR/backups/scripts-$(date +%Y%m%d%H%M%S).tar.gz"
if [[ -n "$(ls -A "$BASE_DIR/scripts" 2>/dev/null || true)" ]]; then
  tar -czf "$BACKUP" -C "$BASE_DIR/scripts" . 2>/dev/null || true
  ok "旧脚本已备份: $BACKUP"
fi
cp -f "$PIPELINE_DIR"/scripts/*.py "$BASE_DIR/scripts/"
chmod 755 "$BASE_DIR"/scripts/*.py
ok "脚本已同步（$(ls "$BASE_DIR"/scripts/*.py | wc -l) 个）"

step "7. 规则与提示词"
for f in audit-rules.md category-rules.md security-rules.md; do
  if [[ -f "$BASE_DIR/config/$f" ]]; then
    ok "保留已有规则: config/$f"
  else
    cp -f "$PIPELINE_DIR/config/$f" "$BASE_DIR/config/$f"
    ok "补写规则: config/$f"
  fi
done
install -d -m 700 "$BASE_DIR/config/cron-prompts"
cp -f "$PIPELINE_DIR"/config/cron-prompts/*.txt "$BASE_DIR/config/cron-prompts/"
cp -f "$PIPELINE_DIR/proposals/TEMPLATE.yaml" "$BASE_DIR/proposals/TEMPLATE.yaml"
ok "cron 提示词与提案模板已更新"

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

step "8. 管理配置"
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
  ok "已重新生成 $CFG"
fi

step "9. Python 依赖"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "已创建 venv: $VENV_DIR"
else
  ok "复用 venv: $VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q -r "$DASHBOARD_DIR/requirements.txt"
ok "依赖已同步"

if (( DO_SCAN )); then
  step "10. 盘点"
  if HERMES_HOME="$HERMES_HOME" "$VENV_DIR/bin/python" "$BASE_DIR/scripts/scan_skills.py" \
      --out-dir "$BASE_DIR/registry" --scan-log "$BASE_DIR/logs/scan-log.md"; then
    ok "盘点完成"
  else
    warn "盘点失败；服务不受影响，可稍后手动重跑"
  fi
else
  note "已指定 --no-scan，跳过盘点"
fi

step "11. 修正属主"
if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]]; then
  chown -R "$RUN_USER":"$RUN_USER" "$BASE_DIR" "$STATE_DIR"
  case "$VENV_DIR" in
    "$REPO_DIR"/*) chown -R "$RUN_USER":"$RUN_USER" "$VENV_DIR" ;;
  esac
  ok "数据目录与仓库内 venv 属主: $RUN_USER"
else
  note "运行用户为 $RUN_USER，无需调整属主"
fi

if [[ -f "$UNIT" ]]; then
  step "12. systemd 单元"
  install -d -m 700 "$BACKUP_DIR"
  cp -a "$UNIT" "$BACKUP_DIR/$SERVICE_NAME.service.$(date +%Y%m%d%H%M%S)"
  ok "旧单元已备份到 $BACKUP_DIR"
  render_template "$DASHBOARD_DIR/deploy/hermes-skill-dashboard.service.in" "$UNIT"
  chmod 644 "$UNIT"
  ok "单元已按当前路径重新渲染"
  if (( DO_RESTART )); then
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME"
    ok "服务已重启"
    if wait_health "$PORT"; then
      ok "健康检查通过: http://127.0.0.1:$PORT/api/health"
    else
      warn "健康检查失败；查看: journalctl -u $SERVICE_NAME -n 50"
      warn "回退: cd $REPO_DIR && git log --oneline -5 && git checkout <上一个提交> && sudo bash update.sh"
    fi
  else
    note "已指定 --no-restart，服务未重启"
  fi
else
  step "12. systemd 单元（无单元，跳过）"
  note "手动运行: cd $REPO_DIR/dashboard && PORT=$PORT ./run.sh"
fi

step "完成"
cat <<EOF
代码位置: $REPO_DIR
管线数据: $BASE_DIR
看板状态: $STATE_DIR
端口: $PORT

查看服务:
  systemctl status $SERVICE_NAME --no-pager
  journalctl -u $SERVICE_NAME -n 50 --no-pager
EOF
