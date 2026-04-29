#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ec2-user/phone_call1}"
DB_PATH="${DB_PATH:-$APP_DIR/cw_store.sqlite}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/usr/share/nginx/html/downloads/fb-poster}"
ASSET_DIR="${ASSET_DIR:-$DOWNLOAD_DIR}"
OUT_ZIP="${OUT_ZIP:-$DOWNLOAD_DIR/fb_poster_bundle.zip}"
TMP_ROOT="${TMPDIR:-/tmp}"
STAMP="$(date +%Y%m%d_%H%M%S)"
WORK_DIR="$TMP_ROOT/fb_poster_bundle_$STAMP"
STAGE_DIR="$WORK_DIR/stage"

mkdir -p "$STAGE_DIR" "$DOWNLOAD_DIR"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

resolve_asset() {
  local name="$1"
  if [[ -e "$ASSET_DIR/$name" ]]; then
    printf '%s\n' "$ASSET_DIR/$name"
    return 0
  fi
  if [[ -e "$APP_DIR/$name" ]]; then
    printf '%s\n' "$APP_DIR/$name"
    return 0
  fi
  return 1
}

for f in fb-poster.d fb-poster.exe fb_poster.pdb fb_poster_sidecar.exe; do
  src="$(resolve_asset "$f" || true)"
  if [[ -z "${src:-}" ]]; then
    echo "Missing required file: $f (looked in ASSET_DIR=$ASSET_DIR and APP_DIR=$APP_DIR)" >&2
    exit 1
  fi
  cp -a "$src" "$STAGE_DIR/"
done

# Create a consistent snapshot of the live SQLite database.
sqlite3 "$DB_PATH" ".backup '$STAGE_DIR/cw_store.sqlite'"

# Optional manifest for support/debugging.
cat > "$STAGE_DIR/manifest.txt" <<MANIFEST
Bundle created: $(date -u '+%Y-%m-%d %H:%M:%S UTC')
Source DB: $DB_PATH
Asset directory: $ASSET_DIR
Included DB snapshot: cw_store.sqlite
Included app files:
- fb-poster.d
- fb-poster.exe
- fb_poster.pdb
- fb_poster_sidecar.exe
MANIFEST

TMP_ZIP="$WORK_DIR/fb_poster_bundle.zip"
(
  cd "$STAGE_DIR"
  zip -r "$TMP_ZIP" fb-poster.d fb-poster.exe fb_poster.pdb fb_poster_sidecar.exe cw_store.sqlite manifest.txt >/dev/null
)

mv "$TMP_ZIP" "$OUT_ZIP"
chmod 644 "$OUT_ZIP"

echo "Wrote bundle: $OUT_ZIP"
