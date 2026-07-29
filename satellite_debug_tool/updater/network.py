"""Updater HTTPS helpers shared by release checks and asset downloads."""
from __future__ import annotations

import ssl
import urllib.request


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
    if not follow_redirects:
        handlers.append(_NoRedirectHandler())
    return urllib.request.build_opener(*handlers)
