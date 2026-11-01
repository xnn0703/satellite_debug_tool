#!/bin/sh
# Certbot 仅在证书确已更新后调用；配置验证成功才交给 systemd reload。
set -eu
if [ -n "${SATELLITE_CERTBOT_HOOK_STATE:-}" ]; then
    printf 'started\n' > "$SATELLITE_CERTBOT_HOOK_STATE"
fi
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx.service
if [ -n "${SATELLITE_CERTBOT_HOOK_STATE:-}" ]; then
    printf 'completed\n' > "$SATELLITE_CERTBOT_HOOK_STATE"
fi
