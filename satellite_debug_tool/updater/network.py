"""Updater HTTPS helpers shared by release checks and asset downloads."""
from __future__ import annotations

import ssl
import urllib.request


def build_https_opener() -> urllib.request.OpenerDirector:
    """Build an opener with an explicit CA bundle when certifi is available."""
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
    )
