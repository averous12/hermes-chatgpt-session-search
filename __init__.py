"""ChatGPT conversation retrieval plugin.

Browser-free: auth is a stored cookie jar (imported once from any browser's
extension export), Cloudflare is cleared via the cf_clearance cookie + Chrome
TLS fingerprint (curl_cffi impersonation). No OAuth, no login automation.

``register(ctx)`` is the standard plugin entry point; ``tools.py`` holds the
schemas + handlers and is also import-light so discovery never pulls in
curl_cffi.
"""

from __future__ import annotations

try:
    from .tools import register_tools
except ImportError:  # pragma: no cover - path-load fallback
    from tools import register_tools


def register(ctx) -> None:
    """Called by the Hermes plugin system on load."""
    register_tools(ctx)
