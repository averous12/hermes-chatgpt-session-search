"""ChatGPT session tools for Hermes (registered via plugins/chatgpt).

Thin handlers over ``client.py``: the stored-cookie-jar ChatGPT backend.
Heavy work (HTTP, FTS) happens in the handler calls, never at import time,
so plugin discovery stays fast and never breaks on a missing jar.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time

from tools.registry import tool_error, tool_result


def _check_available() -> bool:
    """Tools appear only when curl_cffi is importable."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


def _jar_missing_error():
    """Actionable error for the dominant first-run failure: no cookie jar.

    Every tool except ``chatgpt_status`` needs a jar. Raising a bare
    RuntimeError out of the handler surfaces as a traceback in the agent's
    tool result; a tool_error with the exact remediation does not.
    """
    client = _client()
    return tool_error(
        "not logged in: no ChatGPT cookie jar at "
        f"{client.paths().MASTER_JAR}. "
        "Set it up once: (1) log in to chatgpt.com in any browser, "
        "(2) export cookies as JSON with any cookie extension "
        "('Get cookies.txt LOCALLY' or Cookie-Editor), "
        "(3) call chatgpt_import_cookies with the export's path. "
        "Run chatgpt_status to confirm."
    )


def _client():
    """Import the client lazily (it pulls in curl_cffi)."""
    try:
        from . import client
    except ImportError:  # pragma: no cover - path-load fallback
        import client
    return client


def _run_cli(cmd_args):
    """Run a client command, capturing stdout; returns (payload, stderr_text).

    Client commands print human-readable output — tools wrap that into JSON.
    """
    client = _client()
    fn = {
        "status": client.cmd_status,
        "list": client.cmd_list,
        "get": lambda a: client.cmd_get(a, False),
        "get-md": lambda a: client.cmd_get(a, True),
        "sync": client.cmd_sync,
        "find": client.cmd_find,
        "search": client.cmd_search,
    }[cmd_args.cmd]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fn(cmd_args)
        return out.getvalue(), err.getvalue()
    except RuntimeError as e:
        # dead jar / Cloudflare / HTTP errors — actionable message for the agent
        raise


# ---------- schemas ----------

def _schema(name, description, properties, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required)}}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

CHATGPT_STATUS_SCHEMA = _schema(
    "chatgpt_status",
    "Setup/session report — run this FIRST, before any other chatgpt tool. "
    "Returns a checklist of what is and isn't configured: curl_cffi "
    "installed, cookie jar present, session alive, account email, cf_clearance "
    "present, session expiry date, and local index size. On a fresh install it "
    "returns a 'next_step' telling you how to finish setup. On failure it "
    "returns 'error' naming the exact problem. Every other chatgpt tool "
    "returns a plain error if run before setup.",
    {})

CHATGPT_FIND_SCHEMA = _schema(
    "chatgpt_find",
    "Quick hybrid search across the user's ChatGPT conversations (live titles "
    "+ local full-text index + recent unsynced bodies). Results are newest "
    "first with last-activity dates. Use for 'continue our chatgpt talk "
    "about X' / 'did I ask chatgpt about X'. Sub-second when the index "
    "covers the topic; up to ~20s when it falls back to live grepping.",
    {"query": _STR, "recent": _INT, "deep": _BOOL},
    ("query",))

CHATGPT_LIST_SCHEMA = _schema(
    "chatgpt_list",
    "List the user's ChatGPT conversations (id, last-activity date, title), "
    "newest first. Paginate with limit/offset.",
    {"limit": _INT, "offset": _INT})

CHATGPT_GET_SCHEMA = _schema(
    "chatgpt_get",
    "Fetch a ChatGPT conversation as a paginated transcript. Returns the "
    "newest messages as markdown, hard-capped at ~40k chars (~10k tokens) "
    "per page so large conversations never flood context; the oldest "
    "boundary message is truncated with a marker and re-delivered in full "
    "on the previous page (no text is lost). Page backwards with offset "
    "(skip N newest messages), widen/narrow with limit (max 200 messages). "
    "The response header states the window, approximate token cost, and the "
    "exact offset for the previous page. Pass format='json' for the raw API "
    "payload (unpaginated, can be huge — prefer markdown).",
    {"conversation_id": _STR, "format": _STR, "limit": _INT, "offset": _INT,
     "max_chars": _INT},
    ("conversation_id",))

CHATGPT_SYNC_SCHEMA = _schema(
    "chatgpt_sync",
    "Sync the local ChatGPT search index with the server (incremental — only "
    "new/changed conversations). Slow for large backfills (~2.5s per "
    "conversation, paced to respect rate limits); prefer scheduling via cron "
    "over running interactively. --tail N indexes only the N newest messages "
    "per conversation.",
    {"tail": _INT, "limit": _INT})

CHATGPT_IMPORT_COOKIES_SCHEMA = _schema(
    "chatgpt_import_cookies",
    "One-time setup step: import a ChatGPT login from a browser-extension "
    "cookies export (JSON list from 'Get cookies.txt LOCALLY' / Cookie-Editor, "
    "exported while LOGGED IN to chatgpt.com). Browser-agnostic. Stores the "
    "jar at the profile's chatgpt/cookies.master.json, then verifies the "
    "session end-to-end and returns the account email. Re-run this when "
    "chatgpt_status reports the session is dead. Optional params: format='markdown'|"
    "json, index=true to also build the search index.",
    {"path": _STR, "format": _STR, "index": _BOOL},
    ("path",))


# ---------- handlers ----------

def _handle_status(args, **kw):
    """Setup report: is the plugin usable, and if not, what exactly is missing.

    Ordered as a checklist so a first-run agent can act on it directly.
    """
    client = _client()
    payload = {"jar_exists": os.path.exists(client.paths().MASTER_JAR),
               "storage": client.paths().STORAGE}
    # dependency
    try:
        import curl_cffi
        payload["curl_cffi"] = getattr(curl_cffi, "__version__", "unknown")
    except Exception as e:
        payload["curl_cffi"] = None
        payload["error"] = (
            f"dependency missing: curl_cffi not importable ({e}). "
            "Install it into the Hermes venv: "
            "'<hermes venv>/python -m pip install curl_cffi'")
        return tool_result(payload)

    if not payload["jar_exists"]:
        payload["next_step"] = (
            "no cookie jar — log in to chatgpt.com in any browser, export "
            "cookies as JSON, then call chatgpt_import_cookies with the "
            "export's path")
        return tool_result(payload)

    # jar present: how old / when does it expire
    try:
        jar = client.load_jar()
        payload["cookies"] = len(jar)
        payload["cf_clearance"] = any(c["name"] == "cf_clearance" for c in jar)
        exps = [c["expires"] for c in jar
                if "session-token" in c["name"] and c.get("expires")]
        if exps:
            payload["session_expires"] = time.strftime(
                "%Y-%m-%d", time.localtime(min(exps)))
    except Exception as e:
        payload["error"] = f"jar unreadable: {type(e).__name__}: {e}"
        return tool_result(payload)

    try:
        d = client.API().session()
        u = d.get("user") or {}
        payload.update({
            "logged_in": bool(d.get("accessToken")),
            "email": u.get("email"),
            "plan": u.get("chatgpt_plan_type") or u.get("plan_type"),
        })
    except Exception as e:
        payload["logged_in"] = False
        payload["error"] = (f"session dead or Cloudflare-blocked: "
                            f"{type(e).__name__}: {e}. Re-import cookies.")
        return tool_result(payload)

    try:
        import sqlite3
        db = sqlite3.connect(client.paths().INDEX_DB)
        payload["indexed_conversations"] = db.execute(
            "SELECT count(*) FROM convs").fetchone()[0]
        db.close()
    except Exception:
        payload["indexed_conversations"] = 0
    payload["ready"] = True
    # effective tuning (Plugins page → config_schema); env overrides shown too
    payload["settings"] = {
        "pace": client._pace_secs(),
        "cool": client._cool_secs(),
        "impersonate": client._impersonate(),
        "page_chars": int(client._setting_float("page_chars",
                                                "CHATGPT_PAGE_CHARS", 40000)),
        "storage_dir": client.paths().STORAGE,
    }
    return tool_result(payload)


def _handle_find(args, **kw):
    a = argparse.Namespace(
        cmd="find",
        query=str(args.get("query") or "").strip(),
        recent=int(args.get("recent") or 10),
        deep=bool(args.get("deep") or False),
    )
    if not a.query:
        return tool_error("query is required")
    if not os.path.exists(_client().paths().MASTER_JAR):
        return _jar_missing_error()
    out, err = _run_cli(a)
    stages = [l.strip() for l in err.splitlines() if l.strip().startswith("[find]")]
    return tool_result({"query": a.query, "results": out.strip() or "no matches",
                        "stages": stages})


def _handle_list(args, **kw):
    a = argparse.Namespace(cmd="list",
                           limit=int(args.get("limit") or 0),
                           offset=int(args.get("offset") or 0))
    client = _client()
    if not os.path.exists(client.paths().MASTER_JAR):
        return _jar_missing_error()
    items = client.API().conversations()
    if a.offset:
        items = items[a.offset:]
    if a.limit:
        items = items[: a.limit]
    slim = [{"id": i["id"], "title": i.get("title"),
             "update_time": str(i.get("update_time") or "")[:10]}
            for i in items]
    return tool_result({"count": len(slim), "conversations": slim})


def _handle_get(args, **kw):
    cid = str(args.get("conversation_id") or "").strip()
    if not cid:
        return tool_error("conversation_id is required")
    if not os.path.exists(_client().paths().MASTER_JAR):
        return _jar_missing_error()
    fmt = str(args.get("format") or "markdown").lower()
    a = argparse.Namespace(cmd="get" if fmt == "json" else "get-md", cid=cid,
                           limit=int(args.get("limit") or 50),
                           offset=int(args.get("offset") or 0),
                           chars=int(args.get("max_chars") or 0))
    out, _ = _run_cli(a)
    if fmt == "json":
        try:
            return tool_result(json.loads(out))
        except Exception:
            return tool_result({"raw": out})
    return tool_result({"conversation_id": cid, "markdown": out})


def _handle_sync(args, **kw):
    if not os.path.exists(_client().paths().MASTER_JAR):
        return _jar_missing_error()
    a = argparse.Namespace(
        cmd="sync",
        tail=int(args["tail"]) if args.get("tail") not in (None, "") else None,
        limit=int(args.get("limit") or 0),
    )
    out, err = _run_cli(a)
    tail_line = [l for l in out.splitlines() if l.startswith("index:")]
    done_line = [l for l in out.splitlines() if l.startswith("synced")]
    return tool_result({
        "summary": (tail_line + done_line) or out.strip(),
        "throttle": [l.strip() for l in err.splitlines()
                     if l.strip().startswith("[throttle]")],
    })


def _handle_import_cookies(args, **kw):
    path = str(args.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return tool_error(f"cookies export not found: {path!r}")
    try:
        res = _client().import_cookies(path)
    except Exception as e:
        return tool_error(f"import failed: {e}")
    # verify end-to-end
    try:
        d = _client().API().session()
        res["verified"] = bool(d.get("accessToken"))
        res["email"] = (d.get("user") or {}).get("email")
    except Exception as e:
        res["verified"] = False
        res["verify_error"] = f"{type(e).__name__}: {e}"
        res["hint"] = ("jar written but the session didn't verify — the "
                       "export may be stale; re-export while logged in")
        return tool_result(res)

    if res["verified"] and bool(args.get("index")):
        # one-shot setup: import + build the search index immediately, so
        # chatgpt_find is useful without a separate chatgpt_sync call.
        try:
            a = argparse.Namespace(cmd="sync", tail=None, limit=0)
            out, err = _run_cli(a)
            res["index"] = [l for l in out.splitlines()
                            if l.startswith(("index:", "synced"))]
            if not res["index"]:
                res["index"] = out.strip().splitlines()[-1:]
        except Exception as e:
            res["index_error"] = f"{type(e).__name__}: {e}"
    return tool_result(res)


def register_tools(ctx) -> None:
    """Entry point used by the plugin loader when provides_tools is declared."""
    reg = ctx.register_tool
    reg(name="chatgpt_status", toolset="chatgpt",
        schema=CHATGPT_STATUS_SCHEMA, handler=_handle_status,
        check_fn=_check_available, emoji="🔑")
    reg(name="chatgpt_find", toolset="chatgpt",
        schema=CHATGPT_FIND_SCHEMA, handler=_handle_find,
        check_fn=_check_available, emoji="🔎")
    reg(name="chatgpt_list", toolset="chatgpt",
        schema=CHATGPT_LIST_SCHEMA, handler=_handle_list,
        check_fn=_check_available, emoji="📜")
    reg(name="chatgpt_get", toolset="chatgpt",
        schema=CHATGPT_GET_SCHEMA, handler=_handle_get,
        check_fn=_check_available, emoji="📄")
    reg(name="chatgpt_sync", toolset="chatgpt",
        schema=CHATGPT_SYNC_SCHEMA, handler=_handle_sync,
        check_fn=_check_available, emoji="🔄")
    reg(name="chatgpt_import_cookies", toolset="chatgpt",
        schema=CHATGPT_IMPORT_COOKIES_SCHEMA, handler=_handle_import_cookies,
        check_fn=_check_available, emoji="📦")
