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

**Never put the plugin dir on `sys.path`.** Its `tools.py` then shadows Hermes's
own `tools` package, and `from tools.registry import …` dies with
`ModuleNotFoundError: 'tools' is not a package`. Load it as a *package* via
`spec_from_file_location(..., submodule_search_locations=[dir])` — that is what
the real loader does, and what makes the bare-path import work.

```bash
# engine, against the real jar:
python client.py status
python client.py find "timer"
python client.py get-md <conversation-id> --chars 2000   # truncation edge case

# registration smoke test: fake tools.registry + fake ctx, then exec __init__ —
# assert all 6 tools register and check_fn returns True.

# through the real loader (installed copy must be refreshed first, see below):
hermes plugins list | grep chatgpt-session-search
hermes plugins doctor chatgpt-session-search      # expect: 6 tool(s), 0 hook(s)
hermes chat -q "Call chatgpt_status and report the email" -Q
```

Regression script (28 checks: registration, live handlers, fresh-install
errors, import-cookie error paths): `scratch/plugin_regress.py` in the Hermes
cache dir — run it from the plugins dir with the Hermes venv python.

Any claim of "works" needs one of the above, not a read-through.

## Setup contract (do not regress)

Setup usability is a first-class feature here — a new user's first two minutes
decide whether the plugin works.

- **No handler may raise out of the tool boundary.** A missing jar, a corrupt
  jar, a dead session, or a bad export must come back as `tool_error(...)` with
  the *fix* in the message. Tracebacks in tool results are a bug.
- **`chatgpt_status` is the setup report**, ordered as a checklist:
  `curl_cffi` → jar exists → jar readable → session alive → index size.
  It ends with `ready: true`, or an `error`, or a `next_step` on a fresh
  install. Keep those three keys meaningful.
- **Every tool except `chatgpt_status` guards on the jar** via
  `_jar_missing_error()` before touching the network.
- **`chatgpt_import_cookies(index=true)`** does import + verify + index in one
  call, so setup doesn't require the user to know `sync` exists.
- Tool descriptions state real latency/cost, and state when a tool is a setup
  step — the calling agent plans off the description alone.

## Settings (`config_schema`)

Tuning knobs are declared in `plugin.yaml` and rendered as a form on the
Desktop Plugins page. They live at `plugins.entries.<plugin_id>.settings` in
`config.yaml`.

- **Resolution: env var → saved setting → default.** `_setting()` in
  `client.py` implements it. The env var must keep winning: CLI, tests and CI
  override that way without touching `config.yaml` (which agents cannot write).
- **Read lazily, never at import.** `_plugin_settings()` caches one read;
  `reload_settings()` drops it. Discovery imports `tools.py` in every Hermes
  process, so config access must not happen at module scope.
- **Paths are late-bound.** `client.paths()` (`STORAGE`/`MASTER_JAR`/`INDEX_DB`)
  is the authority inside functions; the module-level `STORAGE`/`MASTER_JAR`/
  `INDEX_DB` remain as import-time snapshots for back-compat. A setting changed
  at runtime must be picked up by a *running* process — do not reintroduce a
  captured path inside a function body.
- **Credentials are never settings.** A `json`/`str` field is plaintext in
  `config.yaml`; a cookie export there would be account access. Login stays on
  `chatgpt_import_cookies` + the mode-600 jar. `plugin_regress.py` asserts no
  credential-shaped `config_schema` key exists.
- `surface` these in `chatgpt_status` under `settings` so the effective values
  are always inspectable.

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
