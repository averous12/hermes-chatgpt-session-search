# hermes-plugin-chatgpt

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

## Install

```bash
hermes plugins install <your-github-user>/hermes-plugin-chatgpt
pip install curl_cffi     # the only dependency
```

Then import your ChatGPT login once:

1. Log in to chatgpt.com in **any** browser.
2. Export cookies with any extension (e.g. "Get cookies.txt LOCALLY" or Cookie-Editor) — JSON format.
3. Tell the agent: *"import these cookies into the chatgpt plugin: /path/to/cookies.json"*
   (or run `python3 client.py import-cookies /path/to/cookies.json`).

The jar is stored at `~/.hermes/chatgpt/cookies.master.json` (mode 600). Passwords are never stored.

## Design

- **Session token → access token.** The stored `__Secure-next-auth.session-token` (chunked `.0`/`.1`) is exchanged for a short-lived `accessToken` via `/api/auth/session`; auto-remints on 401.
- **Cloudflare.** `cf_clearance` is bound to a TLS fingerprint, not a live browser — curl_cffi's Chrome impersonation satisfies it on plain HTTP.
- **Search index.** `sync` stores conversation bodies in SQLite FTS5 (`~/.hermes/chatgpt/index.db`) and is incremental via the list endpoint's `update_time`. `find` never waits for sync: it searches live titles + the index, and falls back to live-grepping the most recent unsynced conversations (self-indexing as it goes).
- **Rate limits.** OpenAI enforces a sliding-window budget (~50 body fetches, then 429s for minutes). The client paces body fetches (2.5s default, `CHATGPT_PACE`) and cools off 90s per 429 (`CHATGPT_COOL`) instead of hammering.
- **List cache.** The conversation list is cached 5 minutes locally so `find` pays the multi-page round-trip at most once per window.

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
