#!/usr/bin/env bash
set -u

PUBLIC_BASE="https://foresakenly-figgiest-jazmin.ngrok-free.dev/"
CW_OAUTH_BASE_URL="${CW_OAUTH_BASE_URL:-}"

if [[ -z "$PUBLIC_BASE" && -n "$CW_OAUTH_BASE_URL" ]]; then
  PUBLIC_BASE="$CW_OAUTH_BASE_URL"
fi

APP_DIR="/home/ec2-user/phone_call1"
PID_DIR="$APP_DIR/pids"
LOG_DIR="$APP_DIR/logs"

TWILIO_HOST="0.0.0.0"
TWILIO_PORT="5000"

OAUTH_HOST="127.0.0.1"
OAUTH_PORT="8510"

STREAMLIT_HOST="127.0.0.1"
STREAMLIT_PORT="8509"

NGROK_TARGET="http://localhost:80"

mkdir -p "$PID_DIR" "$LOG_DIR"

ts() {
  date '+[%Y-%m-%d %H:%M:%S]'
}

log() {
  echo "$(ts) $*"
}

is_running_pid() {
  local pid="$1"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  local file="$1"
  [[ -f "$file" ]] && cat "$file" 2>/dev/null || true
}

stop_pidfile() {
  local name="$1"
  local pidfile="$2"
  local pid
  pid="$(read_pid "$pidfile")"

  if [[ -n "$pid" ]] && is_running_pid "$pid"; then
    log "Stopping PID $pid from $pidfile"
    kill "$pid" 2>/dev/null || true
    sleep 1
    if is_running_pid "$pid"; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
}

stop_pattern() {
  local label="$1"
  local pattern="$2"
  local pids

  pids="$(pgrep -f "$pattern" || true)"
  if [[ -n "$pids" ]]; then
    log "Stopping by pattern: $label (pids: $pids)"
    pkill -f "$pattern" 2>/dev/null || true
    sleep 1
    pkill -9 -f "$pattern" 2>/dev/null || true
  fi
}

stop_all() {
  log "Stopping all..."

  stop_pidfile "ngrok" "$PID_DIR/ngrok.pid"
  stop_pidfile "streamlit" "$PID_DIR/streamlit.pid"
  stop_pidfile "oauth" "$PID_DIR/oauth.pid"
  stop_pidfile "twilio" "$PID_DIR/twilio.pid"

  # Use specific patterns only, not generic app.py unless unavoidable.
  stop_pattern "twilio app" "python3 $APP_DIR/app.py"
  stop_pattern "oauth server" "python3 $APP_DIR/gmail_oauth_server.py"
  stop_pattern "streamlit" "streamlit run $APP_DIR/cw_app_phone.py"
  stop_pattern "ngrok" "ngrok http"

  log "Done."
}

start_nginx() {
  log "Starting nginx..."
  sudo systemctl start nginx || true
  sudo systemctl status nginx --no-pager || true
}

start_twilio() {
  log "Starting Twilio webhook server: app.py on ${TWILIO_HOST}:${TWILIO_PORT}"
  cd "$APP_DIR" || exit 1

  nohup python3 "$APP_DIR/app.py" >>"$LOG_DIR/twilio.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/twilio.pid"
  sleep 2

  if is_running_pid "$pid"; then
    log "Twilio webhook pid=$pid. Log: $LOG_DIR/twilio.log"
  else
    log "Twilio failed to stay up. Check: $LOG_DIR/twilio.log"
  fi
}

start_oauth() {
  log "Starting OAuth server: gmail_oauth_server.py on 127.0.0.1:8510"
  cd "$APP_DIR" || exit 1

  export PUBLIC_BASE="$PUBLIC_BASE"

  nohup python3 "$APP_DIR/gmail_oauth_server.py" >>"$LOG_DIR/oauth.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/oauth.pid"
  sleep 3

  if ss -ltnp 2>/dev/null | grep -q '127.0.0.1:8510'; then
    log "OAuth is listening on 127.0.0.1:8510. Log: $LOG_DIR/oauth.log"
  else
    log "OAuth failed to bind 127.0.0.1:8510. Check: $LOG_DIR/oauth.log"
  fi
}

start_streamlit() {
  log "Starting Streamlit: cw_app_phone.py on ${STREAMLIT_HOST}:${STREAMLIT_PORT}"
  cd "$APP_DIR" || exit 1

  nohup streamlit run "$APP_DIR/cw_app_phone.py" \
    --server.address "$STREAMLIT_HOST" \
    --server.port "$STREAMLIT_PORT" \
    >>"$LOG_DIR/streamlit.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/streamlit.pid"
  sleep 3

  if is_running_pid "$pid"; then
    log "Streamlit pid=$pid. Log: $LOG_DIR/streamlit.log"
  else
    log "Streamlit failed to stay up. Check: $LOG_DIR/streamlit.log"
  fi
}

start_ngrok() {
  log "Starting ngrok -> $NGROK_TARGET"
  cd "$APP_DIR" || exit 1

  nohup ngrok http 80 >>"$LOG_DIR/ngrok.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/ngrok.pid"
  sleep 2

  if is_running_pid "$pid"; then
    log "ngrok pid=$pid. Log: $LOG_DIR/ngrok.log"
  else
    log "ngrok failed to stay up. Check: $LOG_DIR/ngrok.log"
  fi
}

status_all() {
  log "=== STATUS ==="

  if systemctl is-active --quiet nginx; then
    log "nginx: active"
  else
    log "nginx: inactive"
  fi

  for svc in twilio oauth streamlit ngrok; do
    pidfile="$PID_DIR/$svc.pid"
    pid="$(read_pid "$pidfile")"
    if [[ -n "$pid" ]] && is_running_pid "$pid"; then
      log "$svc: RUNNING (pid $pid)"
    else
      log "$svc: NOT running"
    fi
  done

  log "Listening ports (best-effort):"
  ss -ltnp 2>/dev/null | grep -E ':80 |:5000 |:8509 |:8510 ' || true
}

case "${1:-start}" in
  stop)
    stop_all
    ;;
  restart)

    stop_all
    log "Starting stack in $APP_DIR"
    start_nginx
    start_twilio
    start_oauth
    start_streamlit
    export PUBLIC_BASE="https://foresakenly-figgiest-jazmin.ngrok-free.dev/"
    start_ngrok
    status_all
    ;;
  start)
    stop_all
    log "Starting stack in $APP_DIR"
    start_nginx
    start_twilio
    start_oauth
    start_streamlit
    start_ngrok
    status_all
    ;;
  status)
    status_all
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac