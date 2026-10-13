---
name: chatgpt-session-search
description: Use when reading the user's ChatGPT history, memories, or profile via the chatgpt_* tools — search/list/fetch conversations, sync the local index, or read what ChatGPT remembers. Covers both memory systems and the auth/setup failure modes.
---

# ChatGPT Session Search

Browser-free access to the user's ChatGPT account: conversation search and
retrieval, plus what ChatGPT remembers about them. Auth is a cookie jar on
disk; Cloudflare is passed with `cf_clearance` + Chrome TLS impersonation.

## Tools

| Tool | Use it for |
|---|---|
| `chatgpt_find` | **Start here for any "find/did I ask about X" question.** Hybrid: live titles + local FTS index + grep of recently-unsynced bodies. Newest-first. |
| `chatgpt_list` | Enumerate conversations (id, date, title) when you need the shape of the history. |
| `chatgpt_get` | One conversation's full transcript (markdown) or raw JSON. Fetch by id from `find`/`list`. |
| `chatgpt_sync` | Refresh the local index. Incremental and rate-limited; schedule it, don't sprawl it. |
| `chatgpt_memories` | What ChatGPT remembers — **both** memory systems (see below). |
| `chatgpt_status` | Session alive? Account email, index size, effective settings. |
| `chatgpt_import_cookies` | Re-auth from a browser cookies export when the session dies. |

## Two memory systems — do not conflate them

`chatgpt_memories` reads two independent stores. Either can be empty for a
given account, so **"no memories" from one is not "no memories"**:

- `kind="summary"` — the newer on-demand synthesis ("what ChatGPT has learned
  about you"), returned as titled narrative sections (Overview, career, work
  areas, preferences...). Generated server-side and **cached behind a checksum**;
  re-reading is cheap. `is_stale: true` means it predates recent activity.
- `kind="legacy"` — discrete saved facts, each with a `source_conversation_id`
  you can feed straight to `chatgpt_get` to see where the fact came from.
- `kind="all"` (default) — both merged. Usually what you want.

Notes that save round trips:
- The final section (often "Dive Deeper") legitimately returns an **empty
  description** — it's a section marker, not a failed fetch. Don't report it as
  missing content.
- `max_tokens: 5000000` is the account's *budget*; `tokens` is what's actually
  used. A count of 0 with a huge max is normal.
- These are read-only. There is no chatgpt tool that writes, edits, or deletes
  ChatGPT memories.

## Setup failures — read `chatgpt_status` first

Any tool returning a "no cookie jar" style error means **not authenticated**,
not "no data". Recovery order:

1. `chatgpt_status` — its checklist says exactly which step failed
   (dependency → jar exists → jar readable → session alive → index size).
2. Re-auth with `chatgpt_import_cookies(path=...)` pointing at a browser cookie
   export scoped to `chatgpt.com`.
3. Session expires periodically — a previously working tool suddenly erroring is
   usually this, not a bug.

The export must contain `__Secure-next-auth.session-token*` and `cf_clearance`.
A jar with the wrong account silently returns that account's data — check the
email in `chatgpt_status` before trusting results.

## Conventions

- `chatgpt_get` markdown output is truncated by the tool's page limit; for very
  long threads pull JSON or page through rather than assuming you have it all.
- Respect the pacing knobs (`pace`, `cool`) when looping `chatgpt_get` over many
  ids — hammering trips rate limits and the sync is incremental for a reason.
- Prefer `find` over walking `list` + `get`: it searches titles, the FTS index,
  and unsynced bodies in one call.
