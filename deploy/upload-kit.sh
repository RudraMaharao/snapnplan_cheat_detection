#!/usr/bin/env bash
# Puts the offline exam kit on S3, so the offline exam page can link to it.
#
# Run it in AWS CloudShell after uploading the kit zip there:
#   curl -fsSLO https://raw.githubusercontent.com/RudraMaharao/snapnplan_cheat_detection/main/deploy/upload-kit.sh
#   bash upload-kit.sh SnapNPlan-Offline-Exam-Kit-v2.2.zip BUCKET-NAME [REGION]
#
# BUCKET-NAME must be unique across all of AWS (lowercase letters, digits and
# hyphens), for example snapnplan-downloads-rm. REGION defaults to Mumbai.
# The bucket only makes files under kit/ public; nothing else in it is.
# At the end it prints one command to paste on the server.
set -euo pipefail

ZIP=${1:-}
BUCKET=${2:-}
REGION=${3:-ap-south-1}
VERSION=${KIT_VERSION:-2.2}
KIT_NAME="SnapNPlan-Offline-Exam-Kit-v$VERSION.zip"

fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }
[[ -n $ZIP && -n $BUCKET ]] || fail "usage: bash upload-kit.sh KIT.zip BUCKET-NAME [REGION]"
[[ -f $ZIP ]] || fail "$ZIP was not found. Upload it with Actions > Upload file first."
[[ $BUCKET =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] ||
  fail "bucket names use lowercase letters, digits and hyphens (3 to 63 characters)."
unzip -tq "$ZIP" >/dev/null || fail "$ZIP is not a complete zip file."
PY_IN_ZIP=$(unzip -Z1 "$ZIP" | grep -m1 -E 'vendor/python/python-[0-9.]+-amd64\.exe$' || true)
[[ -n $PY_IN_ZIP ]] || fail "$ZIP does not contain the Python installer. Is it the offline exam kit?"
PY_NAME=$(basename "$PY_IN_ZIP")

echo "Bucket $BUCKET in $REGION"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [[ $REGION == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null ||
      fail "the bucket could not be created. The name may be taken; try another."
  fi
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false"
POLICY=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"PublicOfflineKit","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::%s/kit/*"}]}' "$BUCKET")
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$POLICY" ||
  fail "public downloads are blocked for this AWS account. In the S3 console open
  'Block Public Access settings for this account', untick the two options about
  bucket policies, save, and run this again."

echo "Uploading the kit"
aws s3 cp "$ZIP" "s3://$BUCKET/kit/$KIT_NAME" --region "$REGION" --only-show-errors \
  --content-type application/zip --content-disposition "attachment; filename=\"$KIT_NAME\""
echo "Uploading the Python installer from inside the kit"
unzip -p "$ZIP" "$PY_IN_ZIP" > "$PY_NAME"
aws s3 cp "$PY_NAME" "s3://$BUCKET/kit/$PY_NAME" --region "$REGION" --only-show-errors \
  --content-type application/octet-stream --content-disposition "attachment; filename=\"$PY_NAME\""
rm -f "$PY_NAME"

URL="https://$BUCKET.s3.$REGION.amazonaws.com/kit"
SHA=$(sha256sum "$ZIP" | cut -d' ' -f1)
SIZE=$(( ($(stat -c %s "$ZIP") + 524288) / 1048576 ))
curl -fsI "$URL/$KIT_NAME" >/dev/null || fail "the kit uploaded but is not publicly downloadable yet. Wait a minute and open $URL/$KIT_NAME"

cat <<EOF

Done. Anyone can now download:
  $URL/$KIT_NAME
  $URL/$PY_NAME

Paste this on the server (in your SSH window):

sudo bash /srv/snapnplan/app/deploy/set-env.sh LAN_KIT_URL=$URL/$KIT_NAME LAN_KIT_PYTHON_URL=$URL/$PY_NAME LAN_KIT_SHA256=$SHA LAN_KIT_SIZE_MB=$SIZE LAN_KIT_VERSION=$VERSION

EOF
