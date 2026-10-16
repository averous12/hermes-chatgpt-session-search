# ChatGPT Session Search

ChatGPT conversation retrieval for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — search, list, and fetch your ChatGPT chats from any Hermes session.

**Browser-free.** No OAuth, no login automation, no headless browser. Auth is a cookie jar you import once from any browser's extension export; Cloudflare passes via the `cf_clearance` cookie + Chrome TLS fingerprint impersonation ([curl_cffi](https://github.com/lexiforest/curl_cffi)).

## Tools

| Tool | What it does |
|---|---|
| `chatgpt_status` | Is the session alive? Account email, index size. |
| `chatgpt_find` | Hybrid quick search: live titles + local FTS index + live grep of recent unsynced bodies. Newest-first. |
| `chatgpt_list` | List conversations (id, last-activity date, title). |
| `chatgpt_get` | One conversation, full transcript (markdown) or raw JSON. |
| `chatgpt_sync` | Incremental index sync (schedule via cron; paced for rate limits). |
| `chatgpt_import_cookies` | Import a login from a browser cookies export. |
| `chatgpt_memories` | What ChatGPT remembers about you — **both** memory systems (new on-demand summary + legacy saved entries). |

### Two memory systems

ChatGPT runs two memory storages side by side, and an account can have either,
both, or neither populated:

| System | Endpoint | Shape |
|---|---|---|
| **new** (on-demand synthesis) | `POST /backend-api/memories/about_you/summary/stream?source=personalization-setting` | Server-Sent Events; titled narrative sections ("Overview", "Karier Dan Riset", …). Generated server-side and cached — keyed on a checksum, so a repeat call is cheap. |
| **legacy** (saved memories) | `GET /backend-api/memories?include_memory_entries=true` | Discrete atomic facts, each with a `conversation_id` back-pointer, plus a token budget. |

`chatgpt_memories` defaults to `kind="all"`, which merges both — usually what
you want for "what do you know about me". Use `kind="summary"` or
`kind="legacy"` to read one. Both are read-only.

> `include_memory_entries=false` is what the ChatGPT settings page uses: it
> returns `memories: []` but keeps the token counters, so "how much memory is
> stored" costs no entry payload. The tool always passes `true`.

## Install

```bash
hermes plugins install averous12/hermes-chatgpt-session-search
```

Install the one dependency **into the Hermes venv** (not your system Python —
Hermes imports plugins with its own interpreter, so `pip install curl_cffi`
in the wrong environment leaves the tools hidden):

```bash
# Windows
"C:/Users/<you>/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -m pip install curl_cffi
# macOS/Linux
~/.hermes/hermes-agent/venv/bin/python -m pip install curl_cffi
```

Confirm the install, then enable the plugin and restart the gateway:

```bash
hermes plugins enable chatgpt-session-search
hermes plugins doctor chatgpt-session-search   # expect: 7 tool(s), 0 hook(s)
hermes gateway restart
```

> **Heads-up:** `hermes plugins enable` takes effect on the next session. In a
> chat that was already open, the tools won't appear until you start a new one.

### Set up the login (once)

1. Log in to chatgpt.com in **any** browser.
2. Export cookies as **JSON** with any cookie extension
   ("Get cookies.txt LOCALLY", Cookie-Editor, …). Export *while logged in*, and
   make sure the export includes `chatgpt.com` — a partial export fails with a
   clear error naming what's missing.
3. Import them, either by telling the agent:

   > *"import these cookies into the chatgpt plugin: /path/to/cookies.json"*

   or from a shell (`client.py` is standalone):

   ```bash
   python client.py import-cookies /path/to/cookies.json
   ```

4. Verify:

   > *"run chatgpt_status"*

   `"ready": true` with your email means you're done. Any problem is reported
   as `error` (with the fix) or `next_step` (on a fresh install) — never a raw
   traceback.

To build the search index at the same time, pass `index=true` to
`chatgpt_import_cookies` (or run `chatgpt_sync` separately; it's paced and
incremental, so prefer scheduling it over running it interactively).

The jar is stored at `$HERMES_HOME/chatgpt/cookies.master.json` (mode 600).
Passwords are never stored.

## Design

- **Session token → access token.** The stored `__Secure-next-auth.session-token` (chunked `.0`/`.1`) is exchanged for a short-lived `accessToken` via `/api/auth/session`; auto-remints on 401.
- **Cloudflare.** `cf_clearance` is bound to a TLS fingerprint, not a live browser — curl_cffi's Chrome impersonation satisfies it on plain HTTP.
- **Search index.** `sync` stores conversation bodies in SQLite FTS5 (`~/.hermes/chatgpt/index.db`) and is incremental via the list endpoint's `update_time`. `find` never waits for sync: it searches live titles + the index, and falls back to live-grepping the most recent unsynced conversations (self-indexing as it goes).
- **Rate limits.** OpenAI enforces a sliding-window budget (~50 body fetches, then 429s for minutes). The client paces body fetches (2.5s default, `CHATGPT_PACE`) and cools off 90s per 429 (`CHATGPT_COOL`) instead of hammering.
- **List cache.** The conversation list is cached 5 minutes locally so `find` pays the multi-page round-trip at most once per window.

## Settings (Plugins page)

The plugin declares a `config_schema`, so the Desktop **Plugins** page renders a
settings form on its row — no file editing needed:

| Setting | Default | Env equivalent |
|---|---|---|
| `storage_dir` | `<hermes home>/chatgpt` | `CHATGPT_PLUGIN_HOME` |
| `pace` | `2.5` | `CHATGPT_PACE` |
| `cool` | `90` | `CHATGPT_COOL` |
| `impersonate` | `chrome145` | `CHATGPT_IMPERSONATE` |
| `user_agent_version` | `145` | `CHATGPT_UA_VERSION` |
| `page_chars` | `40000` | `CHATGPT_PAGE_CHARS` |
| `export_dir` | `<storage_dir>/export` | `CHATGPT_EXPORT_DIR` |

Resolution order is **env var → saved setting → default**, so a shell/CI
override always wins and the form is the fallback. Values are written to
`plugins.entries.chatgpt-session-search.settings` in `config.yaml`; the writer
type-checks each value against the manifest (a string where a number is
declared is rejected).

`chatgpt_status` echoes the effective values under `settings`, so you can always
confirm what's actually in force.

**Credentials are deliberately not a setting.** The cookie jar stays a mode-600
file on disk. A cookie export in `config.yaml` would be plaintext account
access, so the settings form is for tuning only — login stays with
`chatgpt_import_cookies`.

## Troubleshooting

All tools return a JSON `error` string instead of raising, so the calling agent
can read the problem and act. `chatgpt_status` is always the first call.

| Symptom | Cause | Fix |
|---|---|---|
| Tools don't appear at all | `curl_cffi` missing from the **Hermes** venv, or plugin not enabled | Install into the Hermes venv (see Install), `hermes plugins enable …`, restart |
| Tools missing in an already-open chat | `enable` applies next session | Start a new chat |
| `not logged in: no ChatGPT cookie jar at …` | No jar yet | Run the login setup above |
| `session dead or Cloudflare-blocked: …` | Session cookie expired or `cf_clearance` stale | Re-export cookies while logged in, `chatgpt_import_cookies` |
| `no ChatGPT session token in export …` | Export was taken logged-out or scoped to the wrong site | Re-export from chatgpt.com; the error lists the cookies it *did* find |
| `cookies export is empty` | Cookie extension exported nothing (wrong filter/profile) | Check the extension's scope; export the chatgpt.com domain |
| `not a cookies export (expected a JSON list)` | Passed a Netscape `cookies.txt` or another format | Use the extension's **JSON** export, not `cookies.txt` |
| `HTTP 429` / throttle warnings | Sliding-window rate limit (~50 body fetches) | Expected; the client paces and cools off. Let it finish, don't retry-storm |
| `HTTP 403` on `/backend-api/...` | Fingerprint no longer matches | Bump `CHATGPT_IMPERSONATE` to a newer Chrome target (e.g. `chrome146`) |
| `find` is slow / misses recent chats | Index behind; falling back to live grep | Run `chatgpt_sync` (pace it via cron for backfills) |

Quick manual probe, bypassing Hermes entirely:

```bash
cd ~/.hermes/plugins/chatgpt-session-search
python client.py status
python client.py find "some topic"
```

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CHATGPT_PLUGIN_HOME` | `~/.hermes/chatgpt` (profile-aware) | Data dir (jar + index). |
| `CHATGPT_IMPERSONATE` | `chrome145` | curl_cffi TLS/browser fingerprint. |
| `CHATGPT_PACE` | `2.5` | Min seconds between body fetches. |
| `CHATGPT_COOL` | `90` | Cool-off seconds on 429. |

## CLI (debugging)

`client.py` doubles as a standalone CLI:

```bash
python3 client.py status
python3 client.py find "timer" [--recent 10] [--deep]
python3 client.py list [--limit 20]
python3 client.py get-md <conversation-id>
python3 client.py sync [--tail 20]     # tail = index only N newest messages/conv
python3 client.py import-cookies <export.json>
```

## Caveats

- Unofficial API — it's your own data, but OpenAI can change or break these endpoints, and stricter fingerprint checks would require updating `CHATGPT_IMPERSONATE`.
- Cookie jar = account access. Mode 600, never committed, revoke by logging out sessions on chatgpt.com (Settings → Security → "Log out of all devices").
- Session cookie from the export expires per its own `expires` (~months); re-import when `chatgpt_status` reports the session dead.

## License

Apache-2.0.
