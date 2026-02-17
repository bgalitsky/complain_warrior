#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/phone_call}"

# Ports (adjust if you changed anything)
STREAMLIT_PORT="${STREAMLIT_PORT:-8509}"
OAUTH_PORT="${OAUTH_PORT:-8510}"
TWILIO_PORT="${TWILIO_PORT:-5000}"

# Files
STREAMLIT_APP="${STREAMLIT_APP:-cw_app_phone.py}"
OAUTH_APP="${OAUTH_APP:-gmail_oauth_server.py}"
TWILIO_APP="${TWILIO_APP:-app.py}"

LOG_DIR="${LOG_DIR:-$APP_DIR}"
PID_DIR="${PID_DIR:-$APP_DIR/pids}"

mkdir -p "$PID_DIR"

cd "$APP_DIR"

echo "==> Using APP_DIR: $APP_DIR"
echo "==> Logs in:      $LOG_DIR"
echo "==> PIDs in:      $PID_DIR"
echo

# -------- ENV VARS (edit to match your paths) --------
# Public https base (ngrok domain)
export PUBLIC_BASE="${PUBLIC_BASE:-https://foresakenly-figgiest-jazmin.ngrok-free.dev}"

# Gmail token DB and complaint DB
export GMAIL_TOKEN_DB="${GMAIL_TOKEN_DB:-$APP_DIR/cw_gmail_tokens.sqlite}"
export CW_DB_PATH="${CW_DB_PATH:-$APP_DIR/cw_store.sqlite}"

# OAuth client secrets JSON path (edit if different)
export GOOGLE_CLIENT_SECRETS="${GOOGLE_CLIENT_SECRETS:-$APP_DIR/credentials.json}"

# Flask secret (set to something stable)
export FLASK_SECRET_KEY="${FLASK_SECRET_KEY:-change-me-to-a-long-random-string}"

echo "==> PUBLIC_BASE:         $PUBLIC_BASE"
echo "==> GMAIL_TOKEN_DB:      $GMAIL_TOKEN_DB"
echo "==> CW_DB_PATH:          $CW_DB_PATH"
echo "==> GOOGLE_CLIENT_SECRETS $GOOGLE_CLIENT_SECRETS"
echo

# -------- Helpers --------
kill_by_pidfile() {
  local pidfile="$1"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile" || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping PID $pid (from $(basename "$pidfile"))..."
      kill "$pid" 2>/dev/null || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        echo "  still alive, killing -9..."
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$pidfile"
  fi
}

kill_by_pattern() {
  local pat="$1"
  local pids
  pids="$(pgrep -f "$pat" || true)"
  if [[ -n "$pids" ]]; then
    echo "Stopping processes matching: $pat"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    pids="$(pgrep -f "$pat" || true)"
    if [[ -n "$pids" ]]; then
      echo "  still alive, killing -9..."
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
    fi
  fi
}

start_nohup() {
  local name="$1"
  local cmd="$2"
  local logfile="$LOG_DIR/${name}.log"
  local pidfile="$PID_DIR/${name}.pid"

  echo "Starting $name..."
  echo "  $cmd"
  nohup bash -lc "$cmd" >"$logfile" 2>&1 &
  echo $! >"$pidfile"
  echo "  PID $(cat "$pidfile"), log: $logfile"
}

port_listen_check() {
  local port="$1"
  sudo ss -lntp | grep ":$port" >/dev/null 2>&1
}

# -------- Stop old processes --------
echo "==> Stopping old services..."
kill_by_pidfile "$PID_DIR/streamlit.pid"
kill_by_pidfile "$PID_DIR/oauth.pid"
kill_by_pidfile "$PID_DIR/twilio.pid"
kill_by_pidfile "$PID_DIR/ngrok.pid"

# Safety: kill by pattern too (in case pidfiles missing)
kill_by_pattern "streamlit run $STREAMLIT_APP"
kill_by_pattern "python3 .*${OAUTH_APP}"
kill_by_pattern "python3 .*${TWILIO_APP}"
kill_by_pattern "ngrok"

echo

# -------- Start Twilio voice server (Flask on :5000) --------
# Adjust if your app uses a different launch command
start_nohup "twilio" "python3 $TWILIO_APP"

# Wait for port
sleep 1
if port_listen_check "$TWILIO_PORT"; then
  echo "OK: Twilio server listening on $TWILIO_PORT"
else
  echo "WARN: Twilio server not listening on $TWILIO_PORT yet. Check $LOG_DIR/twilio.log"
fi
echo

# -------- Start Gmail OAuth server (Flask on :8510) --------
start_nohup "oauth" "python3 $OAUTH_APP"

sleep 1
if port_listen_check "$OAUTH_PORT"; then
  echo "OK: OAuth server listening on $OAUTH_PORT"
else
  echo "WARN: OAuth server not listening on $OAUTH_PORT yet. Check $LOG_DIR/oauth.log"
fi
echo

# -------- Start Streamlit UI (on :8509) --------
start_nohup "streamlit" "streamlit run $STREAMLIT_APP --server.address 127.0.0.1 --server.port $STREAMLIT_PORT"

sleep 2
if port_listen_check "$STREAMLIT_PORT"; then
  echo "OK: Streamlit listening on $STREAMLIT_PORT"
else
  echo "WARN: Streamlit not listening on $STREAMLIT_PORT yet. Check $LOG_DIR/streamlit.log"
fi
echo

# -------- Start ngrok (OPTIONAL) --------
# If you already run ngrok elsewhere, set START_NGROK=0
START_NGROK="${START_NGROK:-1}"
NGROK_URL="${NGROK_URL:-http://127.0.0.1:80}"

if [[ "$START_NGROK" == "1" ]]; then
  if command -v ngrok >/dev/null 2>&1; then
    start_nohup "ngrok" "ngrok http --url=${PUBLIC_BASE} 80 || ngrok http 80"
    sleep 1
    echo "OK: ngrok started (if configured). See $LOG_DIR/ngrok.log"
  else
    echo "WARN: ngrok not found on PATH. Skipping ngrok start."
  fi
else
  echo "Skipping ngrok start (START_NGROK=0)."
fi

echo
echo "==> Quick local checks:"
echo "  curl -I http://127.0.0.1:${STREAMLIT_PORT}/        (Streamlit direct)"
echo "  curl -I http://127.0.0.1/auth/status               (OAuth status via nginx)"
echo "  curl -I http://127.0.0.1/health                    (Twilio server health via nginx)"
echo
echo "==> Tail logs:"
echo "  tail -f $LOG_DIR/streamlit.log"
echo "  tail -f $LOG_DIR/oauth.log"
echo "  tail -f $LOG_DIR/twilio.log"
echo "  tail -f $LOG_DIR/ngrok.log"
echo
echo "==> Done."
