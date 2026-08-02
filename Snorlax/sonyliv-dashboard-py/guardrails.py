"""Network guardrail for outbound calls to ClickHouse Cloud / LibreChat.

`clickhouse-connect` and `urllib` both honor the standard `*_PROXY` environment
variables. A Bloomberg host shell often has a Bloomberg proxy configured (e.g.
`https_proxy=http://proxy.bloomberg.com:81`) for other, work-related tools. That
proxy leaks into this app's outbound calls — and depending on where you run, it
is either the ONLY working route to the public internet or a dead end:

  * bbvpn / on-prem: direct egress to the internet is blocked; the Bloomberg
    proxy IS the route out. Stripping it makes every ClickHouse Cloud query hang
    and fail. -> we must KEEP the proxy.
  * bbvpn off / off-network: the Bloomberg proxy host isn't resolvable/reachable,
    so any request through it fails. -> we must STRIP the proxy so requests go
    direct.

So we can't decide statically. Instead, for each Bloomberg proxy var we do a
fast TCP reachability probe: if the proxy accepts a connection, keep it; if not,
strip it (the original bbvpn-off behavior). Either way the app finds a working
egress path automatically.

Import this first, before clickhouse_connect / urllib / requests are used.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

_PROXY_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)

# How long to wait when probing whether a proxy is reachable.
_PROBE_TIMEOUT_SEC = 2.0


def _reachable(url: str) -> bool:
    """True if we can open a TCP connection to the proxy host:port quickly."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SEC):
            return True
    except OSError:
        return False


# Probe each distinct proxy URL once, then keep-or-strip every var accordingly.
_probe_cache: dict[str, bool] = {}
for _var in _PROXY_VARS:
    _value = os.environ.get(_var, "")
    if "bloomberg" not in _value.lower():
        continue
    up = _probe_cache.get(_value)
    if up is None:
        up = _probe_cache[_value] = _reachable(_value)
    if not up:
        # Proxy is a dead end (bbvpn off) — remove it so requests go direct.
        del os.environ[_var]
