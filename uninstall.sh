#!/usr/bin/env bash
# hermes-skill-kit uninstaller
#
# 用法:
#   sudo bash uninstall.sh [--keep-venv] [--no-systemd]
#   sudo bash uninstall.sh --purge [--yes]
#
# 默认只停服务、删 systemd 单元、删仓库内的 .venv，数据目录原样保留。
# --purge 才会删除管线数据与看板状态（登录配置、registry、报告、提案、日志、备份）。
# 仓库目录本身和 Hermes 侧的 cron 任务不会被删除。
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${SKD_SERVICE_NAME:-hermes-skill-dashboard}"
UNIT="${SKD_UNIT:-/etc/systemd/system/$SERVICE_NAME.service}"
BACKUP_DIR="${SKD_BACKUP_DIR:-/var/backups/hermes-skill-kit}"

DO_PURGE=0
ASSUME_YES=0
KEEP_VENV=0
WITH_SYSTEMD=1

usage() {
  cat <<'EOF'
用法: sudo bash uninstall.sh [选项]

选项:
  --purge       同时删除数据目录（管线数据 + 看板状态）
  --yes         不交互确认（配合 --purge 用于脚本）
  --keep-venv   保留仓库内的 .venv
  --no-systemd  不碰 systemd 单元（演练用）
  -h, --help    显示本帮助

可用环境变量:
  HERMES_HOME    Hermes 根目录，默认 <用户 home>/.hermes
  SKD_BASE_DIR   管线数据目录，默认 $HERMES_HOME/skill-management
  SKD_STATE_DIR  看板状态目录，默认 $HERMES_HOME/skill-dashboard-data
  SKD_VENV       venv 路径，默认 <仓库>/.venv
  SKD_UNIT       systemd 单元路径，默认 /etc/systemd/system/hermes-skill-dashboard.service（演练用）
EOF
}

while (($#)); do
  case "$1" in
    --purge) DO_PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --keep-venv) KEEP_VENV=1 ;;
    --no-systemd) WITH_SYSTEMD=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

ok()   { printf '  [ OK ] %s\n' "$1"; }
note() { printf '[ .. ] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
step() { printf '\n== %s ==\n' "$1"; }
die()  { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

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
VENV_DIR="${SKD_VENV:-$REPO_DIR/.venv}"

if [[ -f "$UNIT" ]]; then
  u="$(sed -n 's/^Environment=SKD_BASE_DIR=//p' "$UNIT" | head -n1)"
  [[ -n "$u" ]] && [[ -z "${SKD_BASE_DIR:-}" ]] && BASE_DIR="$u"
  u="$(sed -n 's/^Environment=SKD_STATE_DIR=//p' "$UNIT" | head -n1)"
  [[ -n "$u" ]] && [[ -z "${SKD_STATE_DIR:-}" ]] && STATE_DIR="$u"
  u="$(sed -n 's|^ExecStart=\(.*\)/bin/uvicorn.*|\1|p' "$UNIT" | head -n1)"
  [[ -n "$u" ]] && [[ -z "${SKD_VENV:-}" ]] && VENV_DIR="$u"
fi

step "1. 停止服务"
if (( WITH_SYSTEMD )); then
  [[ $EUID -eq 0 ]] || die "操作系统服务需要 root：请用 sudo bash uninstall.sh（演练可加 --no-systemd）"
  if systemctl list-unit-files "$SERVICE_NAME.service" >/dev/null 2>&1 \
     && systemctl list-unit-files "$SERVICE_NAME.service" | grep -q "$SERVICE_NAME.service"; then
    systemctl disable --now "$SERVICE_NAME" || warn "停用服务时出错（可能已停止）"
    ok "服务已停止并取消开机自启"
  else
    note "未安装 $SERVICE_NAME 服务"
  fi
  if [[ -f "$UNIT" ]]; then
    install -d -m 700 "$BACKUP_DIR" 2>/dev/null || true
    if [[ -w "$BACKUP_DIR" ]]; then
      cp -a "$UNIT" "$BACKUP_DIR/$SERVICE_NAME.service.removed-$(date +%Y%m%d%H%M%S)"
      ok "单元副本已保存到 $BACKUP_DIR"
    else
      cp -a "$UNIT" "/tmp/$SERVICE_NAME.service.removed-$(date +%Y%m%d%H%M%S)" || true
      ok "单元副本已保存到 /tmp"
    fi
    rm -f "$UNIT"
    systemctl daemon-reload
    ok "单元已删除"
  fi
else
  note "已指定 --no-systemd，未操作系统服务"
fi

if (( KEEP_VENV )); then
  note "保留 venv: $VENV_DIR"
else
  case "$VENV_DIR" in
    "$REPO_DIR"/.venv|"$REPO_DIR"/.venv/*)
      if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        ok "已删除 venv: $VENV_DIR"
      else
        note "venv 不存在: $VENV_DIR"
      fi ;;
    *)
      note "venv 在仓库之外，未删除: $VENV_DIR"
      ;;
  esac
fi

if (( DO_PURGE )); then
  step "2. 删除数据"
  for d in "$BASE_DIR" "$STATE_DIR"; do
    [[ -n "$d" ]] || die "数据目录为空，拒绝删除"
    [[ "$d" != "/" ]] || die "拒绝删除 /"
    [[ "$d" != "$RUN_HOME" && "$d" != "/root" ]] || die "拒绝删除家目录: $d"
    case "$d" in
      "$HERMES_HOME"/*) ;;
      *) die "数据目录不在 $HERMES_HOME 下，拒绝删除: $d" ;;
    esac
  done

  if (( ! ASSUME_YES )); then
    printf '将永久删除:\n  %s\n  %s\n' "$BASE_DIR" "$STATE_DIR"
    printf '输入 PURGE 确认: '
    read -r answer || true
    [[ "$answer" == "PURGE" ]] || die "未确认，已中止（数据未删除）"
  fi

  for d in "$BASE_DIR" "$STATE_DIR"; do
    if [[ -d "$d" ]]; then
      rm -rf "$d"
      ok "已删除: $d"
    else
      note "不存在，跳过: $d"
    fi
  done
else
  step "2. 数据保留"
  printf '  管线数据: %s\n  看板状态: %s\n' "$BASE_DIR" "$STATE_DIR"
  note "如需一并删除: sudo bash uninstall.sh --purge"
fi

step "完成"
cat <<EOF
未处理的项（需手动决定）:
  - 仓库目录: $REPO_DIR（确认不再更新后可自行删除）
  - Hermes cron 任务：卸载脚本不碰，查看 hermes cron list
  - Hermes 全局配置（$HERMES_HOME/config.yaml）未被修改
EOF
