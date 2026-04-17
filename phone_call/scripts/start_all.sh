#!/usr/bin/env bash
set -u

PUBLIC_BASE="${PUBLIC_BASE:-https://foresakenly-figgiest-jazmin.ngrok-free.dev}"
CW_OAUTH_BASE_URL="${CW_OAUTH_BASE_URL:-}"

ENTRY_FRONTEND_HOST="${ENTRY_FRONTEND_HOST:-0.0.0.0}"
ENTRY_FRONTEND_PORT="${ENTRY_FRONTEND_PORT:-8512}"

SMALL_CLAIMS_HOST="${SMALL_CLAIMS_HOST:-0.0.0.0}"
SMALL_CLAIMS_PORT="${SMALL_CLAIMS_PORT:-8513}"

TWILIO_HOST="${TWILIO_HOST:-0.0.0.0}"
TWILIO_PORT="${TWILIO_PORT:-5000}"

OAUTH_HOST="${OAUTH_HOST:-127.0.0.1}"
OAUTH_PORT="${OAUTH_PORT:-8510}"

STREAMLIT_HOST="${STREAMLIT_HOST:-127.0.0.1}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8509}"

APP_DIR="${APP_DIR:-/home/ec2-user/phone_call1}"
PID_DIR="$APP_DIR/pids"
LOG_DIR="$APP_DIR/logs"

NGROK_TARGET="${NGROK_TARGET:-http://localhost:80}"
NGROK_DOMAIN="${NGROK_DOMAIN:-foresakenly-figgiest-jazmin.ngrok-free.dev}"

if [[ -z "$PUBLIC_BASE" && -n "$CW_OAUTH_BASE_URL" ]]; then
  PUBLIC_BASE="$CW_OAUTH_BASE_URL"
fi
PUBLIC_BASE="${PUBLIC_BASE%/}"

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
    log "Stopping $name PID $pid from $pidfile"
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
  stop_pidfile "small_claims" "$PID_DIR/small_claims.pid"
  stop_pidfile "entry_frontend" "$PID_DIR/entry_frontend.pid"
  stop_pidfile "streamlit" "$PID_DIR/streamlit.pid"
  stop_pidfile "oauth" "$PID_DIR/oauth.pid"
  stop_pidfile "twilio" "$PID_DIR/twilio.pid"

  stop_pattern "twilio app" "python3 $APP_DIR/app.py"
  stop_pattern "oauth server" "python3 $APP_DIR/gmail_oauth_server.py"
  stop_pattern "Complaint Warrior" "streamlit run $APP_DIR/cw_app_phone.py"
  stop_pattern "Complaint Warrior" "python3 -m streamlit run $APP_DIR/cw_app_phone.py"
  stop_pattern "entry frontend" "streamlit run $APP_DIR/entry_frontend.py"
  stop_pattern "entry frontend" "python3 -m streamlit run $APP_DIR/entry_frontend.py"
  stop_pattern "Small Claim Court Warrior" "streamlit run $APP_DIR/small_claim_court_warrior.py"
  stop_pattern "Small Claim Court Warrior" "python3 -m streamlit run $APP_DIR/small_claim_court_warrior.py"
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
  log "Starting OAuth server: gmail_oauth_server.py on ${OAUTH_HOST}:${OAUTH_PORT}"
  cd "$APP_DIR" || exit 1

  export PUBLIC_BASE="$PUBLIC_BASE"

  nohup python3 "$APP_DIR/gmail_oauth_server.py" >>"$LOG_DIR/oauth.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/oauth.pid"
  sleep 3

  if ss -ltnp 2>/dev/null | grep -q "${OAUTH_HOST}:${OAUTH_PORT}"; then
    log "OAuth is listening on ${OAUTH_HOST}:${OAUTH_PORT}. Log: $LOG_DIR/oauth.log"
  else
    log "OAuth failed to bind ${OAUTH_HOST}:${OAUTH_PORT}. Check: $LOG_DIR/oauth.log"
  fi
}

start_complaint_warrior() {
  log "Starting Complaint Warrior: cw_app_phone.py on ${STREAMLIT_HOST}:${STREAMLIT_PORT}"
  cd "$APP_DIR" || exit 1

  nohup python3 -m streamlit run "$APP_DIR/cw_app_phone.py" \
    --server.address "$STREAMLIT_HOST" \
    --server.port "$STREAMLIT_PORT" \
    --server.baseUrlPath complaint_warrior \
    >>"$LOG_DIR/streamlit.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/streamlit.pid"
  sleep 3

  if is_running_pid "$pid"; then
    log "Complaint Warrior pid=$pid. Log: $LOG_DIR/streamlit.log"
  else
    log "Complaint Warrior failed to stay up. Check: $LOG_DIR/streamlit.log"
  fi
}

start_small_claims() {
  log "Starting Small Claim Court Warrior: small_claim_court_warrior.py on ${SMALL_CLAIMS_HOST}:${SMALL_CLAIMS_PORT}"
  cd "$APP_DIR" || exit 1

  nohup python3 -m streamlit run "$APP_DIR/small_claim_court_warrior.py" \
    --server.address "$SMALL_CLAIMS_HOST" \
    --server.port "$SMALL_CLAIMS_PORT" \
    --server.baseUrlPath small_claim_court_warrior \
    >>"$LOG_DIR/small_claims.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/small_claims.pid"
  sleep 3

  if is_running_pid "$pid"; then
    log "Small Claim Court Warrior pid=$pid. Log: $LOG_DIR/small_claims.log"
  else
    log "Small Claim Court Warrior failed to stay up. Check: $LOG_DIR/small_claims.log"
  fi
}

start_entry_frontend() {
  log "Starting entry frontend: entry_frontend.py on ${ENTRY_FRONTEND_HOST}:${ENTRY_FRONTEND_PORT}"
  cd "$APP_DIR" || exit 1

  export CW_PUBLIC_BASE_URL="$PUBLIC_BASE"
  export CW_CUSTOMER_APP_URL="${CW_CUSTOMER_APP_URL:-${PUBLIC_BASE}/complaint_warrior}"
  export CW_COMPANY_APP_URL="${CW_COMPANY_APP_URL:-${PUBLIC_BASE}/complaint_warrior}"
  export CW_SMALL_CLAIMS_APP_URL="${CW_SMALL_CLAIMS_APP_URL:-${PUBLIC_BASE}/small_claim_court_warrior}"

  nohup python3 -m streamlit run "$APP_DIR/entry_frontend.py" \
    --server.address "$ENTRY_FRONTEND_HOST" \
    --server.port "$ENTRY_FRONTEND_PORT" \
    >>"$LOG_DIR/entry_frontend.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/entry_frontend.pid"
  sleep 3

  if is_running_pid "$pid"; then
    log "Entry frontend pid=$pid. Log: $LOG_DIR/entry_frontend.log"
    log "Chooser routes: / -> entry_frontend, /complaint_warrior -> cw_app_phone, /small_claim_court_warrior -> small_claim_court_warrior"
  else
    log "Entry frontend failed to stay up. Check: $LOG_DIR/entry_frontend.log"
  fi
}

start_ngrok() {
  log "Starting ngrok -> $NGROK_TARGET"
  cd "$APP_DIR" || exit 1

  nohup ngrok http --domain="$NGROK_DOMAIN" 80 >>"$LOG_DIR/ngrok.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/ngrok.pid"
  sleep 3

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

  for svc in twilio oauth streamlit entry_frontend small_claims ngrok; do
    pidfile="$PID_DIR/$svc.pid"
    pid="$(read_pid "$pidfile")"
    if [[ -n "$pid" ]] && is_running_pid "$pid"; then
      log "$svc: RUNNING (pid $pid)"
    else
      log "$svc: NOT running"
    fi
  done

  log "Listening ports (best-effort):"
  ss -ltnp 2>/dev/null | grep -E ':80 |:5000 |:8509 |:8510 |:8512 |:8513 ' || true
  log "PUBLIC_BASE=$PUBLIC_BASE"
  log "CW_CUSTOMER_APP_URL=${CW_CUSTOMER_APP_URL:-${PUBLIC_BASE}/complaint_warrior}"
  log "CW_COMPANY_APP_URL=${CW_COMPANY_APP_URL:-${PUBLIC_BASE}/complaint_warrior}"
  log "CW_SMALL_CLAIMS_APP_URL=${CW_SMALL_CLAIMS_APP_URL:-${PUBLIC_BASE}/small_claim_court_warrior}"
}

start_stack() {
  log "Starting stack in $APP_DIR"
  start_nginx
  start_twilio
  start_oauth
  start_complaint_warrior
  start_small_claims
  start_ngrok
  start_entry_frontend
  status_all
}

case "${1:-start}" in
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_stack
    ;;
  start)
    stop_all
    start_stack
    ;;
  status)
    status_all
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
