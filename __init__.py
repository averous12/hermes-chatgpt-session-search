"""ChatGPT conversation retrieval plugin.

Browser-free: auth is a stored cookie jar (imported once from any browser's
extension export), Cloudflare is cleared via the cf_clearance cookie + Chrome
TLS fingerprint (curl_cffi impersonation). No OAuth, no login automation.

``register(ctx)`` is the standard plugin entry point; ``tools.py`` holds the
schemas + handlers and is also import-light so discovery never pulls in
curl_cffi.
"""

from __future__ import annotations

from pathlib import Path

try:
    from .tools import register_tools
except ImportError:  # pragma: no cover - path-load fallback
    from tools import register_tools

SKILL_PATH = Path(__file__).parent / "skills" / "chatgpt-session-search" / "SKILL.md"

SKILL_DESCRIPTION = (
    "Use when reading the user's ChatGPT history, memories, or profile via the "
    "chatgpt_* tools - search/list/fetch conversations, sync the local index, or "
    "read what ChatGPT remembers. Covers both memory systems and the auth/setup "
    "failure modes."
)


def register(ctx) -> None:
    """Called by the Hermes plugin system on load."""
    register_tools(ctx)
    # Ship the usage guidance with the tools so the knowledge and the tools
    # cannot drift apart. Registered read-only: resolvable as
    # 'chatgpt-session-search:chatgpt-session-search' via skill_view(), and
    # listed by skills_list (NOT injected into the system prompt's
    # <available_skills>, so the agent must look it up).
    try:
        ctx.register_skill(
            name="chatgpt-session-search",
            path=SKILL_PATH,
            description=SKILL_DESCRIPTION,
        )
    except Exception:  # pragma: no cover
        # Never let a skill-registration problem stop the tools from loading;
        # the skill is a convenience, the tools are the payload.
        pass
