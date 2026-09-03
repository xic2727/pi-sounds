#!/usr/bin/env bash
# pi-sounds: start / stop / restart / status / tail of the daemon and web UI.
#
# Usage:
#   ./scripts/pi-sounds.sh start      # activate venv, launch daemon + web in background
#   ./scripts/pi-sounds.sh stop       # SIGTERM, then SIGKILL after 5s if needed
#   ./scripts/pi-sounds.sh restart    # stop then start
#   ./scripts/pi-sounds.sh status     # show whether each service is running
#   ./scripts/pi-sounds.sh tail       # follow both log files (Ctrl-C to exit)
#   ./scripts/pi-sounds.sh logs       # list log files with sizes
#
# Override defaults via env vars:
#   APP_DIR=/path/to/pi-sounds  LOG_DIR=/var/log/pi-sounds  ./scripts/pi-sounds.sh start

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
VENV="${VENV:-$APP_DIR/venv}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
PID_DIR="${PID_DIR:-$APP_DIR/run}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8601}"

DAEMON_NAME="pi-sounds-daemon"
WEB_NAME="pi-sounds-web"
DAEMON_LOG="$LOG_DIR/daemon.log"
WEB_LOG="$LOG_DIR/web.log"
DAEMON_PID="$PID_DIR/daemon.pid"
WEB_PID="$PID_DIR/web.pid"

VENV_PY="$VENV/bin/python"
VENV_BIN_DIR="$VENV/bin"

mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

is_alive() {
    local pidfile="$1"
    [[ -f "$pidfile" ]] || return 1
    local pid
    pid=$(cat "$pidfile" 2>/dev/null || echo "")
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
    local pidfile="$1"
    [[ -f "$pidfile" ]] && cat "$pidfile" || echo "?"
}

start_one() {
    local name="$1" pidfile="$2" logfile="$3"
    shift 3
    if is_alive "$pidfile"; then
        log "$name already running (pid=$(read_pid "$pidfile"))"
        return 0
    fi
    rm -f "$pidfile"
    [[ -x "$VENV_PY" ]] || die "venv python not found at $VENV_PY (run: python3 -m venv \"$VENV\" && venv/bin/pip install -r requirements.txt)"
    log "starting $name ..."
    # Activate venv in the same shell so the background process inherits
    # PATH/VIRTUAL_ENV. cd to APP_DIR so relative imports work, then
    # nohup so the process keeps running after this script exits.
    (
        set -e
        # shellcheck disable=SC1091
        source "$VENV/bin/activate"
        cd "$APP_DIR"
        export PYTHONPATH="$APP_DIR"
        export PI_SOUNDS_DATA="${PI_SOUNDS_DATA:-$HOME/.local/share/pi-sounds/data}"
        nohup "$@" >>"$logfile" 2>&1 &
        echo $! > "$pidfile"
    )
    sleep 0.5
    if is_alive "$pidfile"; then
        log "$name started (pid=$(read_pid "$pidfile"), log=$logfile)"
    else
        die "$name failed to start; last lines of $logfile:
$(tail -n 20 "$logfile" 2>/dev/null || echo '(log empty)')"
    fi
}

stop_one() {
    local name="$1" pidfile="$2"
    if ! [[ -f "$pidfile" ]]; then
        log "$name: no pid file, nothing to stop"
        return 0
    fi
    local pid
    pid=$(read_pid "$pidfile")
    if ! kill -0 "$pid" 2>/dev/null; then
        log "$name: pid $pid not alive (stale pid file)"
        rm -f "$pidfile"
        return 0
    fi
    log "stopping $name (pid=$pid) ..."
    kill "$pid" 2>/dev/null || true
    for _ in {1..50}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        log "$name did not exit in 5s; sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
        sleep 0.3
    fi
    rm -f "$pidfile"
    log "$name stopped"
}

status_one() {
    local name="$1" pidfile="$2" logfile="$3"
    if is_alive "$pidfile"; then
        local pid started
        pid=$(read_pid "$pidfile")
        started=$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//' || echo "?")
        echo "  $name: RUNNING  pid=$pid  started='$started'  log=$logfile"
    else
        echo "  $name: STOPPED  log=$logfile"
    fi
}

# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------

start_daemon() {
    start_one "$DAEMON_NAME" "$DAEMON_PID" "$DAEMON_LOG" \
        "$VENV_PY" -m daemon.main
}

start_web() {
    start_one "$WEB_NAME" "$WEB_PID" "$WEB_LOG" \
        "$VENV_BIN_DIR/streamlit" run web/app.py \
        --server.address "$HOST" \
        --server.port "$PORT" \
        --server.headless true \
        --browser.gatherUsageStats false
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_start() {
    start_daemon
    start_web
    log "all services up. logs: $LOG_DIR  (tail -f to follow)"
}

cmd_stop() {
    stop_one "$WEB_NAME" "$WEB_PID"
    stop_one "$DAEMON_NAME" "$DAEMON_PID"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    echo "pi-sounds status:"
    status_one "$DAEMON_NAME" "$DAEMON_PID" "$DAEMON_LOG"
    status_one "$WEB_NAME" "$WEB_PID" "$WEB_LOG"
}

cmd_tail() {
    local files=()
    [[ -f "$DAEMON_LOG" ]] && files+=("$DAEMON_LOG")
    [[ -f "$WEB_LOG"    ]] && files+=("$WEB_LOG")
    if [[ ${#files[@]} -eq 0 ]]; then
        die "no log files yet; start the services first"
    fi
    exec tail -n 50 -F "${files[@]}"
}

cmd_logs() {
    ls -lh "$LOG_DIR" || true
    echo "---"
    echo "pid files:"
    ls -l "$PID_DIR" 2>/dev/null || true
}

cmd_help() {
    sed -n '2,/^# ---/p' "$0" | sed 's/^# \?//'
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

cmd="${1:-}"
case "$cmd" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    tail)    cmd_tail ;;
    logs)    cmd_logs ;;
    -h|--help|help|"") cmd_help ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|tail|logs}" >&2
        exit 2
        ;;
esac