"""Shared SSL context for httpx clients.

Building a default SSL context loads the Windows system certificate store,
which blocks the event loop for hundreds of milliseconds. httpx does this
eagerly in every ``AsyncClient()`` constructor, so creating one client per
node froze the loop for ~55s during a 100-node batch test and mass-timed-out
healthy nodes. Build the context once and pass it as ``verify=``.
"""

from __future__ import annotations

import ssl
from functools import lru_cache

__all__ = ["shared_ssl_context"]


@lru_cache(maxsize=1)
def shared_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()
