#!/usr/bin/env bash
# Sets up SnapNPlan on a fresh Ubuntu 24.04 server (an AWS EC2 instance).
#
#   curl -fsSLO https://raw.githubusercontent.com/RudraMaharao/snapnplan_cheat_detection/main/deploy/setup-server.sh
#   sudo bash setup-server.sh
#
# It asks a few questions first, then installs everything, gets a free HTTPS
# certificate and starts the app. Running it again updates the code and keeps
# every setting you gave it. Step-by-step guide: deploy/DEPLOY-AWS.md.
#
# Answers can also be passed in, to skip the questions:
#   sudo DOMAIN=snapnplan.ddns.net LE_EMAIL=you@example.com EXAM_PASSCODE=... bash setup-server.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/RudraMaharao/snapnplan_cheat_detection.git}"
BRANCH="${BRANCH:-main}"
APP_USER=snapnplan
BASE=/srv/snapnplan
APP=$BASE/app
DATA=$BASE/data
VENV=$BASE/venv
ENV_FILE=$APP/.env
WEBROOT=/var/www/certbot
declare -A PENDING=()

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() { printf '\nSetup stopped: %s\n' "$*" >&2; exit 1; }

# Runs a command as the app user, with the www-data group that nginx reads with.
as_app() {
  sudo -u "$APP_USER" -g www-data -H -- \
    bash -c 'cd /srv/snapnplan && umask 027 && exec "$@"' bash "$@"
}

env_get() {
  [[ -f $ENV_FILE ]] || return 0
  sed -nE "s/^$1=['\"]?([^'\"]*)['\"]?\$/\1/p" "$ENV_FILE" | tail -n 1
}
env_has() { [[ -f $ENV_FILE ]] && grep -q "^$1=" "$ENV_FILE"; }
env_set() {
  local key=$1 value=$2 tmp
  tmp=$(mktemp)
  if [[ -f $ENV_FILE ]]; then grep -v "^$key=" "$ENV_FILE" > "$tmp" || true; fi
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  install -o "$APP_USER" -g "$APP_USER" -m 600 "$tmp" "$ENV_FILE"
  rm -f "$tmp"
}

# ask KEY "Question" [secret] [min-length]: keeps a value already in .env,
# otherwise uses an environment variable of the same name, otherwise asks.
# A min-length makes the answer required (and typed twice when secret).
ask() {
  local key=$1 question=$2 secret=${3:-} min=${4:-0} value second
  if env_has "$key"; then return 0; fi
  value=${!key:-}
  while true; do
    if [[ -z $value ]]; then
      if [[ $secret == secret ]]; then read -r -s -p "  $question: " value; echo
      else read -r -p "  $question: " value; fi
    fi
    if [[ $value == *"'"* || $value == *'"'* ]]; then
      note "Quotes cannot be used here. Try again."; value=""; continue
    fi
    if (( min > 0 )); then
      if (( ${#value} < min )); then
        note "This needs at least $min characters."; value=""; continue
      fi
      if [[ $secret == secret && -z ${!key:-} ]]; then
        read -r -s -p "  Type it again: " second; echo
        [[ $value == "$second" ]] || { note "Those did not match. Try again."; value=""; continue; }
      fi
    fi
    break
  done
  PENDING[$key]=$value
}

imds() {   # EC2 instance metadata (IMDSv2); empty when not on EC2
  local token value
  token=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
            -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null) || return 0
  value=$(curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" \
            "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null) || return 0
  printf '%s' "$value"
}

render_nginx() {   # http or https
  sed -e "s|__DOMAIN__|$DOMAIN|g" "$APP/deploy/nginx/snapnplan-$1.conf" \
    > /etc/nginx/sites-available/snapnplan
  ln -sf /etc/nginx/sites-available/snapnplan /etc/nginx/sites-enabled/snapnplan
  rm -f /etc/nginx/sites-enabled/default
  nginx -t -q
  systemctl reload-or-restart nginx
}

[[ $EUID -eq 0 ]] || fail "run it with sudo: sudo bash $0"
# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || fail "this script is for Ubuntu 24.04."

# ---- questions first, so the long install can run on its own ------------
say "A few questions"
DOMAIN=${DOMAIN:-$(env_get DJANGO_ALLOWED_HOSTS)}
DOMAIN=${DOMAIN%%,*}
if [[ -z $DOMAIN ]]; then
  read -r -p "  Web address of the site [snapnplan.ddns.net]: " DOMAIN
  DOMAIN=${DOMAIN:-snapnplan.ddns.net}
fi
DOMAIN=$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')
[[ $DOMAIN =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || fail "\"$DOMAIN\" is not a web address."
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
email=${LE_EMAIL:-}
if [[ ! -f $CERT && -z $email ]]; then
  read -r -p "  Email for HTTPS certificate notices (Enter to skip): " email
fi
ask EXAM_PASSCODE "Staff passcode for exam mode (at least 6 characters)" secret 6
if ! env_has GEMINI_API_KEY; then note "API keys for writing quizzes. Leave any you do not use empty."; fi
ask GEMINI_API_KEY "Gemini API key" secret
ask GROQ_API_KEY "Groq API key" secret
ask MISTRAL_API_KEY "Mistral API key" secret

# ---- install ---------------------------------------------------------------
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q python3-venv git curl nginx certbot tesseract-ocr >/dev/null

if ! swapon --show | grep -q .; then
  say "Adding 2 GB of swap space"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# The app user's home is the data folder, so gunicorn and TensorFlow can keep
# their small working files there.
id -u "$APP_USER" >/dev/null 2>&1 ||
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$APP_USER"
install -d -o root -g root -m 755 "$BASE"
install -d -o "$APP_USER" -g www-data -m 750 "$APP" "$VENV" "$DATA" "$DATA/media" "$DATA/staticfiles"
install -d -m 755 "$WEBROOT"

if [[ -d $APP/.git ]]; then
  say "Updating the code"
  as_app git -C "$APP" fetch --quiet origin "$BRANCH"
  as_app git -C "$APP" checkout --quiet "$BRANCH"
  as_app git -C "$APP" merge --quiet --ff-only "origin/$BRANCH"
else
  [[ -z $(ls -A "$APP") ]] || fail "$APP has files in it but is not a git checkout."
  say "Downloading SnapNPlan"
  as_app git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP"
fi

say "Installing Python packages (TensorFlow is large, this takes a few minutes)"
[[ -x $VENV/bin/python ]] || as_app python3 -m venv "$VENV"
as_app env PIP_NO_CACHE_DIR=1 "$VENV/bin/pip" install --quiet --upgrade pip
as_app env PIP_NO_CACHE_DIR=1 "$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"

say "Saving settings"
[[ -n $(env_get DJANGO_SECRET_KEY) ]] ||
  env_set DJANGO_SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
env_set DJANGO_DEBUG 0
env_set DJANGO_ALLOWED_HOSTS "$DOMAIN"
env_set DJANGO_CSRF_TRUSTED_ORIGINS "https://$DOMAIN"
env_set SNAPNPLAN_DATA_DIR "$DATA"
env_has DJANGO_HTTPS || env_set DJANGO_HTTPS 0
for key in "${!PENDING[@]}"; do env_set "$key" "${PENDING[$key]}"; done
for key in LAN_KIT_URL LAN_KIT_PYTHON_URL LAN_KIT_SHA256 LAN_KIT_SIZE_MB; do
  env_has "$key" || env_set "$key" ""
done
note "Saved in $ENV_FILE (readable only by the app)."

say "Preparing the database and static files"
as_app "$VENV/bin/python" "$APP/manage.py" migrate --noinput >/dev/null
as_app "$VENV/bin/python" "$APP/manage.py" collectstatic --noinput --clear >/dev/null
chmod 600 "$DATA/db.sqlite3"

say "Starting the app"
install -m 644 "$APP/deploy/snapnplan.service" /etc/systemd/system/snapnplan.service
systemctl daemon-reload
systemctl enable --quiet snapnplan
systemctl restart snapnplan

if [[ -f $CERT ]]; then
  render_nginx https
else
  render_nginx http
  say "Getting the HTTPS certificate"
  PUBLIC_IP=$(imds public-ipv4)
  [[ $PUBLIC_IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || PUBLIC_IP=""
  DNS_IP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}' || true)
  if [[ -n $PUBLIC_IP && $DNS_IP != "$PUBLIC_IP" ]]; then
    note "$DOMAIN points to ${DNS_IP:-no address yet}, but this server is $PUBLIC_IP."
    note "In No-IP, set $DOMAIN to $PUBLIC_IP, wait five minutes, then run:"
    note "  sudo bash $APP/deploy/setup-server.sh"
    exit 0
  fi
  if [[ -n $email ]]; then contact=(--email "$email"); else contact=(--register-unsafely-without-email); fi
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --agree-tos \
      "${contact[@]}" --deploy-hook "systemctl reload nginx" --quiet ||
    fail "the certificate was not issued. Check that HTTP and HTTPS are allowed in the security group and that $DOMAIN points to this server, then run this again."
  render_nginx https
fi
env_set DJANGO_HTTPS 1
systemctl restart snapnplan

say "Checking the site"
code=""
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "https://$DOMAIN/" || true)
  [[ $code == 200 ]] && break
  sleep 2
done
[[ $code == 200 ]] || fail "https://$DOMAIN/ answered ${code:-nothing}. See: sudo journalctl -u snapnplan -n 50"

say "SnapNPlan is live at https://$DOMAIN"
if [[ -z $(env_get LAN_KIT_URL) ]]; then
  note "The offline exam page has no download link yet. Run deploy/upload-kit.sh in"
  note "AWS CloudShell, then paste the command it prints here."
fi
note "To update after you push changes: sudo bash $APP/deploy/update.sh"
