#!/usr/bin/env bash
# Changes one or more settings in the server's .env file and restarts SnapNPlan.
#
#   sudo bash /srv/snapnplan/app/deploy/set-env.sh KEY=value [KEY=value ...]
#
# Examples:
#   sudo bash /srv/snapnplan/app/deploy/set-env.sh EXAM_PASSCODE=new-passcode
#   sudo bash /srv/snapnplan/app/deploy/set-env.sh LAN_KIT_URL=https://... LAN_KIT_SHA256=...
set -euo pipefail
ENV_FILE=/srv/snapnplan/app/.env

[[ $EUID -eq 0 ]] || { echo "Run it with sudo: sudo bash $0 KEY=value" >&2; exit 1; }
[[ $# -gt 0 ]] || { echo "Usage: sudo bash $0 KEY=value [KEY=value ...]" >&2; exit 1; }
[[ -f $ENV_FILE ]] || { echo "$ENV_FILE is missing. Run setup-server.sh first." >&2; exit 1; }

for pair in "$@"; do
  key=${pair%%=*}
  value=${pair#*=}
  [[ $pair == *=* && $key =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "Not a KEY=value pair: $pair" >&2; exit 1; }
  [[ $value != *"'"* && $value != *'"'* && $value != *$'\n'* ]] ||
    { echo "$key cannot contain quotes or a line break." >&2; exit 1; }
  tmp=$(mktemp)
  grep -v "^$key=" "$ENV_FILE" > "$tmp" || true
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  install -o snapnplan -g snapnplan -m 600 "$tmp" "$ENV_FILE"
  rm -f "$tmp"
  echo "Set $key"
done
systemctl restart snapnplan
echo "SnapNPlan restarted."
