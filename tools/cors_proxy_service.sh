#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# EdgeLane CORS proxy service manager
#
# Wraps tools/cors_proxy.py so it can run as a long-lived background process
# under WSL (or any Linux), with start/stop/restart/status, log tailing, and
# an installer that wires it into WSL auto-start.
#
# Usage:
#   cors_proxy_service.sh start              # spawn proxy if not running
#   cors_proxy_service.sh stop               # kill it
#   cors_proxy_service.sh restart            # stop + start
#   cors_proxy_service.sh status             # show running state, PID, port
#   cors_proxy_service.sh logs               # tail -f the log file
#   cors_proxy_service.sh install            # auto-start on WSL boot
#                                            # (systemd if available, else
#                                            #  /etc/wsl.conf boot.command)
#   cors_proxy_service.sh uninstall          # remove auto-start hook
#   cors_proxy_service.sh help
#
# Environment overrides:
#   EDGELANE_CORS_PORT   default 8787
#   EDGELANE_CORS_BIND   default 127.0.0.1
#
# State lives in ~/.edgelane/ (PID file + log).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SCRIPT="$SCRIPT_DIR/cors_proxy.py"
STATE_DIR="${HOME}/.edgelane"
PID_FILE="$STATE_DIR/cors_proxy.pid"
LOG_FILE="$STATE_DIR/cors_proxy.log"
SERVICE_NAME="edgelane-cors-proxy"
PORT="${EDGELANE_CORS_PORT:-8787}"
BIND="${EDGELANE_CORS_BIND:-127.0.0.1}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  G='\033[32m'; R='\033[31m'; Y='\033[33m'; D='\033[2m'; N='\033[0m'
else
  G=''; R=''; Y=''; D=''; N=''
fi

mkdir -p "$STATE_DIR"

die()  { printf "${R}error:${N} %s\n" "$*" >&2; exit 1; }
info() { printf "${D}%s${N}\n" "$*"; }
ok()   { printf "${G}✓${N} %s\n" "$*"; }
warn() { printf "${Y}!${N} %s\n" "$*"; }

is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

pid_or_blank() { [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null || true; }

port_in_use() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -ltn "sport = :$PORT" 2>/dev/null | tail -n +2 | grep -q .
}

require_proxy() {
  [ -f "$PROXY_SCRIPT" ] || die "proxy script not found at $PROXY_SCRIPT"
  command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"
}

cmd_start() {
  require_proxy
  # If a systemd unit is installed, defer to it — that's the canonical owner.
  if has_systemd && systemd_unit_exists; then
    if systemd_unit_active; then
      ok "already running via systemd (use \`systemctl --user status ${SERVICE_NAME}\`)"
      return 0
    fi
    info "delegating to systemd: systemctl --user start ${SERVICE_NAME}"
    systemctl --user start "${SERVICE_NAME}.service"
    sleep 0.4
    if systemd_unit_active; then
      ok "started via systemd"
    else
      die "systemctl start failed — see: journalctl --user -u ${SERVICE_NAME} -n 30"
    fi
    return 0
  fi
  if is_running; then
    ok "already running (PID $(pid_or_blank), $BIND:$PORT)"
    return 0
  fi
  if port_in_use; then
    warn "port $PORT is already in use by another process"
    command -v ss >/dev/null 2>&1 && ss -ltnp "sport = :$PORT" 2>/dev/null | tail -n +2
    die "free the port, set EDGELANE_CORS_PORT to something else, or run: pkill -f cors_proxy.py"
  fi
  setsid nohup python3 "$PROXY_SCRIPT" --port "$PORT" --bind "$BIND" \
    > "$LOG_FILE" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" > "$PID_FILE"
  sleep 0.4
  if ! is_running; then
    rm -f "$PID_FILE"
    printf "${R}✗ failed to start.${N} last log lines:\n"
    [ -f "$LOG_FILE" ] && tail -20 "$LOG_FILE"
    exit 1
  fi
  ok "started (PID $pid) — listening on http://$BIND:$PORT"
  info "logs: $LOG_FILE"
}

cmd_stop() {
  # If managed by systemd, stop via systemd
  if has_systemd && systemd_unit_exists; then
    if systemd_unit_active; then
      info "stopping via systemd: systemctl --user stop ${SERVICE_NAME}"
      systemctl --user stop "${SERVICE_NAME}.service"
      ok "stopped (systemd-managed)"
    else
      info "systemd unit inactive"
    fi
    # Also clean up any stray PID-file process so coproxy is consistent
    if is_running; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    return 0
  fi
  if ! is_running; then
    # PID file missing but maybe a stray process is on the port — try to find it
    if port_in_use; then
      local stray
      stray=$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
      if [ -n "$stray" ]; then
        warn "found untracked process on port $PORT (PID $stray) — killing"
        kill "$stray" 2>/dev/null || true
        sleep 0.3
        kill -9 "$stray" 2>/dev/null || true
        ok "stopped (was untracked)"
        return 0
      fi
    fi
    info "not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  kill "$pid" 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6; do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "graceful stop timed out — sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  ok "stopped"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  # Check systemd first — if the unit is the owner, report its state
  if has_systemd && systemd_unit_exists; then
    if systemd_unit_active; then
      local sd_pid
      sd_pid=$(systemctl --user show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || echo unknown)
      printf "${G}● running (systemd)${N}\n"
      printf "  unit    : %s\n" "${SERVICE_NAME}.service"
      printf "  pid     : %s\n" "$sd_pid"
      printf "  listen  : %s:%s\n" "$BIND" "$PORT"
      printf "  manage  : systemctl --user {status,stop,restart} %s\n" "${SERVICE_NAME}"
      printf "  logs    : journalctl --user -u %s -f\n" "${SERVICE_NAME}"
    else
      printf "${Y}○ stopped (systemd unit installed but inactive)${N}\n"
      printf "  unit    : %s\n" "${SERVICE_NAME}.service"
      printf "  start   : systemctl --user start %s\n" "${SERVICE_NAME}"
    fi
  elif is_running; then
    local pid started
    pid=$(cat "$PID_FILE")
    started=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//' || echo unknown)
    printf "${G}● running${N}\n"
    printf "  pid     : %s\n" "$pid"
    printf "  listen  : %s:%s\n" "$BIND" "$PORT"
    printf "  started : %s\n" "$started"
    printf "  log     : %s\n" "$LOG_FILE"
  elif port_in_use; then
    local stray
    stray=$(ss -ltnp "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    printf "${Y}● running (UNTRACKED)${N}\n"
    printf "  pid     : %s\n" "${stray:-?}"
    printf "  listen  : %s:%s\n" "$BIND" "$PORT"
    printf "  note    : not managed by this script — run \`coproxy stop\` to kill it\n"
  else
    printf "${Y}○ stopped${N}\n"
    printf "  log     : %s\n" "$LOG_FILE"
  fi
  if is_running && command -v curl >/dev/null 2>&1; then
    if curl -sf -o /dev/null -X OPTIONS "http://$BIND:$PORT/gemini/models" \
        -H 'Access-Control-Request-Method: POST'; then
      printf "  health  : ${G}OK${N} (CORS preflight returned 204)\n"
    else
      printf "  health  : ${R}probe failed${N} (proxy alive but not responding)\n"
    fi
  fi
}

cmd_logs() {
  [ -f "$LOG_FILE" ] || { info "no log file yet at $LOG_FILE"; return 0; }
  exec tail -f "$LOG_FILE"
}

has_systemd() {
  systemctl --version >/dev/null 2>&1 || return 1
  [ -d /run/systemd/system ] || return 1
  return 0
}

# Is our user-level systemd unit installed?
systemd_unit_exists() {
  [ -f "$HOME/.config/systemd/user/${SERVICE_NAME}.service" ]
}

# Is systemd actively managing it (active or activating)?
systemd_unit_active() {
  systemctl --user is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null
}

cmd_install() {
  if has_systemd; then install_systemd; else install_wslconf; fi
}

install_systemd() {
  info "systemd detected — installing user service"
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  local unit="$unit_dir/${SERVICE_NAME}.service"
  cat > "$unit" <<EOF
[Unit]
Description=EdgeLane CORS proxy for browser to Atlas and Gemini
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 $PROXY_SCRIPT --port $PORT --bind $BIND
Restart=on-failure
RestartSec=3
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}.service"
  systemctl --user start "${SERVICE_NAME}.service" || true
  ok "installed user systemd service: $unit"
  info "for boot-without-login, run once:  sudo loginctl enable-linger \$USER"
  info "manage with:  systemctl --user {start,stop,restart,status} ${SERVICE_NAME}"
}

install_wslconf() {
  info "no systemd — installing via /etc/wsl.conf boot.command (needs sudo)"
  local boot_cmd="$SCRIPT_DIR/cors_proxy_service.sh start"
  local awk_script
  awk_script=$(mktemp)
  cat > "$awk_script" <<'AWK'
BEGIN { boot=0; inserted=0 }
{
  if ($0 ~ /^\[boot\]/) { boot=1; print; next }
  if ($0 ~ /^\[/ && boot==1 && inserted==0) {
    print "command = " CMD; inserted=1; boot=0
  }
  if (boot==1 && $0 ~ /^command[[:space:]]*=/) {
    print "command = " CMD; inserted=1; next
  }
  print
}
END {
  if (boot==1 && inserted==0) print "command = " CMD
  if (boot==0 && inserted==0) { print ""; print "[boot]"; print "command = " CMD }
}
AWK
  sudo touch /etc/wsl.conf
  sudo awk -v CMD="$boot_cmd" -f "$awk_script" /etc/wsl.conf | sudo tee /etc/wsl.conf.new >/dev/null
  sudo mv /etc/wsl.conf.new /etc/wsl.conf
  rm -f "$awk_script"
  ok "added boot.command to /etc/wsl.conf — will run on next WSL start"
  info "to apply now from Windows PowerShell:  wsl --shutdown  (then reopen WSL)"
  cmd_start
}

cmd_uninstall() {
  local unit="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
  if [ -f "$unit" ]; then
    systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
    rm -f "$unit"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "removed systemd unit"
  fi
  if grep -qF "cors_proxy_service.sh" /etc/wsl.conf 2>/dev/null; then
    sudo sed -i '/cors_proxy_service\.sh/d' /etc/wsl.conf
    ok "removed boot.command line from /etc/wsl.conf"
  fi
  cmd_stop
}

cmd_help() {
  awk '/^# ─/ && ++n==1 {flag=1; next} /^# ─/ && n==2 {exit} flag {sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"
}

case "${1:-help}" in
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  -h|--help|help) cmd_help ;;
  *) die "unknown subcommand: $1 (try: start, stop, restart, status, logs, install, uninstall)" ;;
esac
