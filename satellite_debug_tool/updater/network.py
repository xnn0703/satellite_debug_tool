"""Updater HTTPS helpers shared by release checks and asset downloads."""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request


class HttpsEndpointError(urllib.error.URLError):
    """服务器重定向没有满足 HTTPS 入口合同。"""


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    # 在默认 redirect handler 之前验证，HTTP 请求不会先发出再被拒绝。
    handler_order = 400

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HttpsEndpointError("update redirect requires an HTTPS endpoint")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_https_opener(
    *,
    follow_redirects: bool = True,
) -> urllib.request.OpenerDirector:
    """Build an opener with an explicit CA bundle when certifi is available."""
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    handlers = [urllib.request.HTTPSHandler(context=context)]
    handlers.append(HttpsOnlyRedirectHandler() if follow_redirects else _NoRedirectHandler())
    return urllib.request.build_opener(*handlers)
