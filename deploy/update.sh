#!/usr/bin/env bash
# Brings the server up to date with the code on GitHub and restarts SnapNPlan.
#
#   sudo bash /srv/snapnplan/app/deploy/update.sh
#
# Your settings, database and uploads are kept.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run it with sudo: sudo bash $0" >&2; exit 1; }
cd /srv/snapnplan
sudo -u snapnplan -g www-data -H -- git -C /srv/snapnplan/app pull --quiet --ff-only
exec bash /srv/snapnplan/app/deploy/setup-server.sh
