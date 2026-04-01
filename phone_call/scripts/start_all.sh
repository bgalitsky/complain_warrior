#!/usr/bin/env bash
# start_all.sh — start/stop/restart/status for Complaint Warrior stack (nohup)
# Components:
#  - nginx (port 80) reverse proxy
#  - gmail_oauth_server.py (Flask) on 127.0.0.1:8510
#  - Streamlit UI on 127.0.0.1:8509
#  - ngrok tunnel to port 80 (optional) for PUBLIC_BASE
#
# DOES NOT change Gmail auth logic — it just starts your existing servers.

  export PUBLIC_BASE="https://foresakenly-figgiest-jazmin.ngrok-free.dev"
  export CW_OAUTH_BASE_URL="https://foresakenly-figgiest-jazmin.ngrok-free.dev"
  export GMAIL_TOKEN_DB="/home/ec2-user/phone_call/cw_gmail_tokens.sqlite"
  export GOOGLE_CLIENT_SECRETS="/home/ec2-user/phone_call/credentials.json"
  export FLASK_SECRET_KEY="some-long-random-string"
  export CW_DB_PATH="/home/ec2-user/phone_call/cw_multiuser.sqlite"

set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/phone_call}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8509}"
OAUTH_PORT="${OAUTH_PORT:-8510}"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
PID_DIR="${PID_DIR:-$APP_DIR/pids}"

# Your files (adjust if you renamed them)
STREAMLIT_FILE="${STREAMLIT_FILE:-cw_app_phone.py}"
OAUTH_FILE="${OAUTH_FILE:-gmail_oauth_server.py}"

# ngrok (optional)
ENABLE_NGROK="${ENABLE_NGROK:-1}"          # 1 or 0
NGROK_BIN="${NGROK_BIN:-ngrok}"
NGROK_URL="${NGROK_URL:-}"                # e.g. https://foresakenly-figgiest-jazmin.ngrok-free.dev  (preferred)
NGROK_REGION="${NGROK_REGION:-us}"        # optional
# If you don't have a reserved URL, leave NGROK_URL empty and the script will try to read the public URL from ngrok API.

mkdir -p "$LOG_DIR" "$PID_DIR"

ts() { date +"%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "ERROR: missing command: $1"; exit 1; }
}

is_running_pidfile() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] || return 1
  local pid; pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

kill_pidfile() {
  local pidfile="$1"
  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      log "Stopping PID $pid from $pidfile"
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$pidfile" || true
  fi
}

kill_by_pattern() {
  local pat="$1"
  local pids
  pids="$(pgrep -f "$pat" || true)"
  if [[ -n "$pids" ]]; then
    log "Stopping by pattern: $pat  (pids: $pids)"
    pkill -f "$pat" || true
    sleep 1
    pkill -9 -f "$pat" || true
  fi
}

start_nginx() {
  log "Starting nginx..."
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start nginx || true
    sudo systemctl status nginx --no-pager || true
  else
    sudo service nginx start || true
    sudo service nginx status || true
  fi
}

start_oauth() {
  need_cmd python3
  cd "$APP_DIR"

  local pidfile="$PID_DIR/oauth.pid"
  if is_running_pidfile "$pidfile"; then
    log "OAuth server already running (pid $(cat "$pidfile"))."
    return 0
  fi

  # Avoid "Address already in use"
  kill_by_pattern "$OAUTH_FILE" || true

  log "Starting OAuth server: $OAUTH_FILE on $BIND_ADDR:$OAUTH_PORT"
  # Ensure OAuth runs on localhost (your nginx should proxy /auth/* and /oauth2callback to it)
  nohup env \
    OAUTH_BIND_ADDR="$BIND_ADDR" \
    OAUTH_PORT="$OAUTH_PORT" \
    python3 "$OAUTH_FILE" \
    > "$LOG_DIR/oauth.log" 2>&1 &

  echo $! > "$pidfile"
  sleep 1
  log "OAuth pid=$(cat "$pidfile"). Log: $LOG_DIR/oauth.log"
}

start_streamlit() {
  need_cmd streamlit
  cd "$APP_DIR"

  local pidfile="$PID_DIR/streamlit.pid"
  if is_running_pidfile "$pidfile"; then
    log "Streamlit already running (pid $(cat "$pidfile"))."
    return 0
  fi

  kill_by_pattern "streamlit run $STREAMLIT_FILE" || true

  log "Starting Streamlit: $STREAMLIT_FILE on $BIND_ADDR:$STREAMLIT_PORT"
  nohup streamlit run "$STREAMLIT_FILE" \
    --server.address "$BIND_ADDR" \
    --server.port "$STREAMLIT_PORT" \
    > "$LOG_DIR/streamlit.log" 2>&1 &

  echo $! > "$pidfile"
  sleep 1
  log "Streamlit pid=$(cat "$pidfile"). Log: $LOG_DIR/streamlit.log"
}

start_ngrok() {
  if [[ "$ENABLE_NGROK" != "1" ]]; then
    log "ngrok disabled (ENABLE_NGROK=0)."
    return 0
  fi

  need_cmd "$NGROK_BIN"

  local pidfile="$PID_DIR/ngrok.pid"
  if is_running_pidfile "$pidfile"; then
    log "ngrok already running (pid $(cat "$pidfile"))."
    return 0
  fi

  kill_by_pattern "$NGROK_BIN http" || true

  log "Starting ngrok -> http://localhost:80"
  if [[ -n "$NGROK_URL" ]]; then
    # new syntax: --url
    nohup "$NGROK_BIN" http --region="$NGROK_REGION" --url="$NGROK_URL" 80 \
      > "$LOG_DIR/ngrok.log" 2>&1 &
  else
    # Let ngrok assign a URL
    nohup "$NGROK_BIN" http --region="$NGROK_REGION" 80 \
      > "$LOG_DIR/ngrok.log" 2>&1 &
  fi
  echo $! > "$pidfile"
  sleep 2
  log "ngrok pid=$(cat "$pidfile"). Log: $LOG_DIR/ngrok.log"

  # Try to discover public URL and write PUBLIC_BASE for convenience
  # (Your UI uses PUBLIC_BASE/auth/start — we are NOT changing auth; just helping you set env var.)
  if command -v curl >/dev/null 2>&1; then
    local tunnel_json
    tunnel_json="$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null || true)"
    if [[ -n "$tunnel_json" ]]; then
      local pub
      pub="$(python3 - <<'PY' 2>/dev/null || true
import json,sys
j=json.load(sys.stdin)
t=j.get("tunnels") or []
https=[x.get("public_url","") for x in t if x.get("public_url","").startswith("https://")]
print(https[0] if https else (t[0].get("public_url","") if t else ""))
PY
<<<"$tunnel_json")"
      if [[ -n "${pub:-}" ]]; then
        log "Detected ngrok public URL: $pub"
        echo "$pub" > "$APP_DIR/PUBLIC_BASE.txt"
        log "Wrote $APP_DIR/PUBLIC_BASE.txt (export PUBLIC_BASE=\$(cat PUBLIC_BASE.txt) before starting Streamlit if needed)"
      fi
    fi
  fi
}

status() {
  log "=== STATUS ==="
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl is-active nginx >/dev/null 2>&1 && log "nginx: active" || log "nginx: inactive"
  fi

  for svc in oauth streamlit ngrok; do
    local pidfile="$PID_DIR/$svc.pid"
    if is_running_pidfile "$pidfile"; then
      log "$svc: RUNNING (pid $(cat "$pidfile"))"
    else
      log "$svc: NOT running"
    fi
  done

  log "Listening ports (best-effort):"
  if command -v ss >/dev/null 2>&1; then
    ss -lntp | egrep ":80|:$STREAMLIT_PORT|:$OAUTH_PORT" || true
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lntp 2>/dev/null | egrep ":80|:$STREAMLIT_PORT|:$OAUTH_PORT" || true
  fi
}

stop_all() {
  log "Stopping all..."
  kill_pidfile "$PID_DIR/ngrok.pid"
  kill_pidfile "$PID_DIR/streamlit.pid"
  kill_pidfile "$PID_DIR/oauth.pid"

  # Fallback by pattern (in case pidfiles stale)
  kill_by_pattern "streamlit run $STREAMLIT_FILE" || true
  kill_by_pattern "$OAUTH_FILE" || true
  kill_by_pattern "$NGROK_BIN http" || true

  log "Done."
}

start_all() {
  log "Starting stack in $APP_DIR"
  start_nginx
  start_oauth
  start_streamlit
  start_ngrok
  status
  log "Logs: $LOG_DIR  |  PIDs: $PID_DIR"
}

cmd="${1:-start}"
case "$cmd" in
  start)   start_all ;;
  stop)    stop_all ;;
  restart) stop_all; start_all ;;
  status)  status ;;
  *) echo "Usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac