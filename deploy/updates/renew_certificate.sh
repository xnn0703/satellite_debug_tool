#!/bin/sh
# Certbot hook 的失败不保证影响 renew 退出码，单独核对本次 hook 的完成事实。
set -eu
: "${CERTBOT_CERT_NAME:?configure /etc/satellite-updates/certbot.env}"
certbot=/opt/satellite-updates/certbot/bin/certbot
hook=/opt/satellite-updates/tools/deploy/updates/renewed_reload_nginx.sh
SATELLITE_CERTBOT_HOOK_STATE=/run/satellite-updates-certbot/hook-state
export SATELLITE_CERTBOT_HOOK_STATE
test -x "$certbot"
test -r "$hook"
test -w /run/satellite-updates-certbot
rm -f "$SATELLITE_CERTBOT_HOOK_STATE"
result=0
"$certbot" renew --non-interactive --quiet --cert-name "$CERTBOT_CERT_NAME" \
    --deploy-hook "/bin/sh $hook" || result=$?
if [ -e "$SATELLITE_CERTBOT_HOOK_STATE" ] \
    && [ "$(cat "$SATELLITE_CERTBOT_HOOK_STATE")" != completed ]; then
    echo 'certificate renewed; nginx validation/reload hook failed; inspect this unit journal' >&2
    exit 1
fi
exit "$result"
