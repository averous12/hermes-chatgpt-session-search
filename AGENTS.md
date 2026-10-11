# AGENTS.md — ChatGPT Session Search (Hermes plugin)

Standalone Hermes plugin: retrieve the user's ChatGPT conversations from any
Hermes session. Browser-free — auth is a stored cookie jar + Chrome TLS
impersonation (`curl_cffi`). No OAuth, no browser automation, no obscura.

## Layout

| File | Role |
|---|---|
| `plugin.yaml` | Manifest. `name: chatgpt-session-search` is the registry key (must match the installed dir name). Declares `provides_tools`. |
| `__init__.py` | `register(ctx)` → `register_tools(ctx)`. Import-light on purpose. |
| `tools.py` | Schemas (`_schema`), handlers (`_handle_*`), `register_tools(ctx)`. Must NOT import `curl_cffi` or `client` at module level — discovery imports this file in every Hermes process. |
| `client.py` | The engine. Also a standalone CLI (`python3 client.py <cmd>`). Heavy imports live here. |

Dual import path is intentional (`try: from .x import … except ImportError: from x import …`):
the loader may load the plugin as a package or by file path.

## Auth model (read before touching `client.py`)

- Jar: `$HERMES_HOME/chatgpt/cookies.master.json`, mode 600, profile-aware via
  `hermes_constants.get_hermes_home()`; override with `CHATGPT_PLUGIN_HOME`.
  **Never commit it, never log its contents.** `.gitignore` covers jar/index/export.
- Import path: browser-extension JSON export (WebExtensions cookies-API shape) →
  `import_cookies()`. Keeps only chatgpt.com/openai.com scope, int-casts
  `expirationDate` (floats have broken cookie loaders before), requires a
  `__Secure-next-auth.session-token*` cookie (chunked `.0`/`.1` preserved).
- Requests: `curl_cffi` with `CHATGPT_IMPERSONATE` (chrome145). Cloudflare passes
  because `cf_clearance` is bound to the TLS fingerprint, not a live browser.
- Token: `accessToken` minted from `/api/auth/session`; `_get` remints once on
  401/403. No token is ever persisted — it's short-lived and derivable.

## Server behavior — hard-won, do not re-learn

- **Rate limit:** sliding window, ~50 body fetches, then `429 {"detail":"Too many
  requests"}` for minutes. Hammering *extends* the block. Policy: `PACE` (2.5s
  between body fetches, `CHATGPT_PACE`) + one 90s cool-off per 429 (`CHATGPT_COOL`),
  no retry storms. Batch helpers (`fetch_many`) go through the same `_get`.
- **`total` in the list endpoint lies** (reports limit+1). Paginate until an empty
  page; never plan work off `total`.
- **`update_time`**: ISO string in list responses, float epoch in some conversation
  payloads. Compare/stash as `str(...)`, format defensively.
- `/backend-api/search` is dead (404) and `query=` on the list endpoint is silently
  ignored — that's why search is local (FTS5) + title-scan, not server-side.
- Conversation payload = full message tree (`mapping` + `current_node`). There is
  **no server-side tail/pagination** — partial views are cut client-side.
- Session cookies rotate server-side; the jar is read fresh per run, so rotations
  persist only if you write them back.

## Pagination contract (`chatgpt_get`)

Message sizes vary ~100× (code dumps vs "ok"), so pages are bounded by
**chars first, count second**: newest-first walk, `limit` (≤200, default 50),
hard cap `PAGE_CHARS` (40k ≈ 10k tokens, `CHATGPT_PAGE_CHARS`/`max_chars`).

Invariants when changing this:
1. Newest content survives; the **oldest** boundary message is the one truncated.
2. A truncated message is **not** counted in the next `offset` — it re-arrives in
   full on the previous page. Losing this loses text between pages.
3. The header states window, token estimate, truncation, and the exact next
   `offset`. Agents navigate by it; keep it truthful.
4. `format=json` stays raw and unpaginated (escape hatch only).

## Search/index (`index.db`, SQLite FTS5)

- Tables: `convs` (id, title, update_time, tail), `bodies`, `conv_fts` (FTS5,
  unicode61), `list_cache` (conversation list, `LIST_TTL` 300s — the list
  pagination was the dominant latency before this cache).
- `find` = live title scan + FTS + live-grep fallback over recent unsynced convs
  (self-indexing as it goes). Fallback only on **zero** hits (`--deep` forces).
- Results sort by `update_time` desc, not bm25 — "continue the discussion about X"
  means the *recent* conversation, not the most lexical one.
- Staleness = stored `update_time` mismatch **or** stored `tail` narrower than
  requested. This lets `sync --tail N` widen later without poisoning the index.
- `sync` is paced and incremental; prefer cron over interactive runs for backfills.

## Verifying changes (no pytest suite yet)

```bash
# engine, against the real jar:
python3 client.py status
python3 client.py find "timer"
python3 client.py get-md <conversation-id> --chars 2000   # truncation edge case

# registration smoke test: fake tools.registry + fake ctx, then exec __init__ —
# assert all 6 tools register and check_fn returns True.

# through the real loader (installed copy must be refreshed first, see below):
hermes plugins list | grep chatgpt-session-search
hermes chat -q "Call chatgpt_status and report the email" -Q
```

Any claim of "works" needs one of the above, not a read-through.

## Deploy = copy (two trees, one truth)

- Truth: this repo. Installed: `~/.hermes/plugins/chatgpt-session-search/`
  (a copy with `.git` stripped).
- After editing here: re-copy + strip `.git`, else the live plugin runs stale code.
  The enabled entry in `~/.hermes/config.yaml` (`plugins.enabled`) must match the
  directory name — renaming the plugin means renaming dir + manifest + config.
- `config.yaml` is guarded against agent patch tools; edit via terminal.

## Conventions

- Handlers return `tool_result(...)` / `tool_error(...)` JSON strings — never bare text.
- Tool descriptions state real latency/cost ("sub-second when indexed; up to ~20s
  on fallback") so the calling agent can plan.
- Unofficial API: endpoints break silently. When something 404s, probe the live
  site (in-page `fetch` or curl) before rewriting code — the answer is usually a
  changed endpoint, not a bug in our client.
