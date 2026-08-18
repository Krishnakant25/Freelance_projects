"""
Network tool. Default-deny egress with an allow-list, plus SSRF protection.

Doc §6.1 calls cutting egress "the single highest-value control and it's free" —
because it removes one leg of the lethal trifecta outright. An agent that can
read private data and is exposed to untrusted content still cannot exfiltrate if
it has nowhere to send it.

`Capabilities.check_egress()` does the enforcement: the host must be explicitly
allow-listed AND must not resolve to a private/loopback/link-local address. The
second check matters because an allow-listed hostname whose DNS an attacker
controls could otherwise point at 169.254.169.254 and hand over cloud
credentials.
"""
import logging

import requests

from .. import config
from ..capabilities import Capabilities, Capability

logger = logging.getLogger(__name__)


def fetch(caps: Capabilities, url: str, method: str = "GET") -> dict:
    """Fetches a URL. Response bodies are returned flagged as UNTRUSTED.

    A fetched web page is the archetypal injection vector — it is content an
    attacker can author. It must never re-enter the agent loop as instructions;
    see agent.py, where only the ReaderAgent (which holds no privileged tools)
    is allowed to look at it.
    """
    host = caps.check_egress(url)

    if method.upper() not in ("GET", "HEAD"):
        # Write-shaped HTTP verbs are how data leaves. Restricting to reads is
        # a meaningful reduction even within an allow-listed host.
        raise PermissionError(
            f"only GET/HEAD are permitted; {method.upper()} could exfiltrate data"
        )

    resp = requests.request(
        method.upper(), url,
        timeout=config.EGRESS_TIMEOUT_SECONDS,
        stream=True,
        allow_redirects=False,  # a redirect could bounce to a non-allow-listed host
    )

    if resp.is_redirect or resp.is_permanent_redirect:
        location = resp.headers.get("Location", "")
        # Re-check the redirect target against the same policy rather than
        # following it blindly — otherwise the allow-list is one hop deep.
        try:
            caps.check_egress(location)
        except Exception as e:  # noqa: BLE001
            resp.close()
            raise PermissionError(
                f"redirect to {location!r} refused: {e}"
            ) from e

    body = b""
    for chunk in resp.iter_content(8192):
        body += chunk
        if len(body) > config.EGRESS_MAX_BYTES:
            resp.close()
            raise ValueError(
                f"response exceeded the {config.EGRESS_MAX_BYTES:,} byte cap"
            )
    resp.close()

    return {
        "url": url,
        "host": host,
        "status": resp.status_code,
        "bytes": len(body),
        "content": body.decode("utf-8", errors="replace"),
        "trust": "untrusted",
    }
