#!/usr/bin/env python3
"""ChatGPT session retrieval over plain HTTP — no browser.

Auth: the plugin-owned cookie jar (cookies.master.json, imported once from any
browser export). Cloudflare is cleared via the cf_clearance cookie + a Chrome
TLS fingerprint (curl_cffi impersonation), so requests never need a browser.

Usage:
  chatgpt_http.py status
  chatgpt_http.py list [--limit N]
  chatgpt_http.py get <conversation_id>
  chatgpt_http.py get-md <conversation_id>
  chatgpt_http.py find <query> [--recent N]
  chatgpt_http.py search <query> [--limit N]      # index only, no network
  chatgpt_http.py sync [--tail N]
  chatgpt_http.py export [--out DIR]
"""
import argparse, json, os, re, sqlite3, sys, time
from curl_cffi import requests as cr

def _hermes_home() -> str:
    """Profile-safe Hermes home — never hardcode ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser("~/.hermes")


# Plugin settings (`plugin.yaml` → `config_schema`) land under
# ``plugins.entries.<plugin_id>.settings`` and are edited from the Desktop
# Plugins page. Resolution order per knob: env var (explicit override, used by
# tests and CLI) → saved plugin setting → default. Read lazily, never at import,
# so the plugin still discovers when config.yaml is absent or unreadable.
PLUGIN_ID = "chatgpt-session-search"
_SETTINGS_CACHE = [None]


def _plugin_settings() -> dict:
    """``plugins.entries.chatgpt-session-search.settings`` ({} when unavailable)."""
    if _SETTINGS_CACHE[0] is not None:
        return _SETTINGS_CACHE[0]
    block = {}
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        if isinstance(cfg, dict):
            entries = cfg.get("plugins", {})
            if isinstance(entries, dict):
                entries = entries.get("entries", {})
            if isinstance(entries, dict):
                entry = entries.get(PLUGIN_ID, {})
            else:
                entry = {}
            if isinstance(entry, dict):
                s = entry.get("settings", {})
                if isinstance(s, dict):
                    block = s
    except Exception:
        block = {}
    _SETTINGS_CACHE[0] = block
    return block


def reload_settings() -> None:
    """Drop the settings cache (used after a settings write, and by tests)."""
    _SETTINGS_CACHE[0] = None


def _setting(name, env_var, default):
    """env override → saved plugin setting → default."""
    raw = os.environ.get(env_var)
    if raw not in (None, ""):
        return raw
    val = _plugin_settings().get(name)
    if val in (None, ""):
        return default
    return val


def _setting_float(name, env_var, default):
    try:
        return float(_setting(name, env_var, default))
    except (TypeError, ValueError):
        return float(default)


def _storage_dir():
    return os.environ.get(
        "CHATGPT_PLUGIN_HOME",
        _setting("storage_dir", "CHATGPT_PLUGIN_HOME",
                 os.path.join(_hermes_home(), "chatgpt")))


class _Paths:
    """Late-bound paths: STORAGE/MASTER_JAR/INDEX_DB follow the current settings."""

    @property
    def STORAGE(self):
        return _storage_dir()

    @property
    def MASTER_JAR(self):
        return os.path.join(_storage_dir(), "cookies.master.json")

    @property
    def INDEX_DB(self):
        return os.path.join(_storage_dir(), "index.db")


_paths = _Paths()
STORAGE = _paths.STORAGE          # import-time snapshot (back-compat)
MASTER_JAR = os.path.join(STORAGE, "cookies.master.json")
INDEX_DB = os.path.join(STORAGE, "index.db")
BASE = "https://chatgpt.com"


def _impersonate():
    return _setting("impersonate", "CHATGPT_IMPERSONATE", "chrome145")


def _ua():
    ver = str(_setting("user_agent_version", "CHATGPT_UA_VERSION", "145"))
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36")


IMPERSONATE = _impersonate()      # import-time snapshot (back-compat)
UA = _ua()


def paths():
    """Current paths object — authoritative when settings may have changed."""
    return _paths


def load_jar():
    jar = _paths.MASTER_JAR
    if not os.path.exists(jar):
        raise RuntimeError(
            f"no cookie jar at {jar} — import one with "
            f"`chatgpt_import_cookies <path to cookies export>`")
    with open(jar) as f:
        return json.load(f)


def make_session():
    """HTTP session carrying the whole stored jar; returns (session, device_id)."""
    jar = load_jar()
    ck = "; ".join(f"{c['name']}={c['value']}" for c in jar
                   if c["domain"].lstrip(".").endswith("chatgpt.com"))
    did = next((c["value"] for c in jar if c["name"] == "oai-did"), "")
    s = cr.Session(impersonate=_impersonate())
    s.headers.update({
        "User-Agent": _ua(), "Cookie": ck, "Oai-Device-Id": did,
        "Oai-Language": "en-US", "Referer": BASE + "/",
        "Origin": BASE, "Accept": "application/json",
    })
    return s, did


class API:
    """Thin backend-api client with token minting + 401 re-mint."""

    def __init__(self, token=None):
        self.s, self.did = make_session()
        self.tok = token

    def _mint(self):
        r = self.s.get(f"{BASE}/api/auth/session", timeout=30)
        d = json.loads(r.text)
        self.tok = d.get("accessToken")
        if not self.tok:
            raise RuntimeError("no accessToken — session cookie dead or "
                               "Cloudflare challenge (re-import cookies)")
        return self.tok

    def _get(self, path, timeout=60, retry=True, paced=False):
        if paced:
            _pace()
        if self.tok is None:
            self._mint()
        h = {"Authorization": f"Bearer {self.tok}"}
        r = self.s.get(f"{BASE}{path}", headers=h, timeout=timeout)
        if r.status_code == 429 and retry:
            _cool(f"429 on {path}")
            return self._get(path, timeout=timeout, retry=False, paced=paced)
        if r.status_code in (401, 403) and retry:
            self._mint()          # token rotated -> remint once and retry
            return self._get(path, timeout=timeout, retry=False, paced=paced)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
        return json.loads(r.text)

    def session(self):
        return json.loads(self.s.get(f"{BASE}/api/auth/session", timeout=30).text)

    def conversations(self, per_page=100, fresh=False):
        """Full list — paginate until an empty page. NOTE: the API's `total`
        field is unreliable (reports limit+1), so never trust it.

        Cached in the local index db for LIST_TTL seconds so `find` never
        pays the multi-page round-trip on the hot path."""
        if not fresh:
            cached = _list_cache_load()
            if cached is not None:
                return cached
        out, offset = [], 0
        while True:
            d = self._get(f"/backend-api/conversations?limit={per_page}"
                           f"&offset={offset}&order=updated")
            items = d.get("items") or []
            out.extend(items)
            offset += len(items)
            if not items:
                break
        _list_cache_save(out)
        return out

    def conversation(self, cid, paced=True):
        return self._get(f"/backend-api/conversation/{cid}", timeout=90,
                         paced=paced)


def fetch_many(cids, token=None, workers=8):
    """Fetch several conversations concurrently (own session per worker).
    Returns {cid: data}; failures are dropped silently."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    def one(cid):
        api = API(token=token)
        return cid, api.conversation(cid)   # _get handles pace + one cool-off
    errs = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, c) for c in cids]
        for f in as_completed(futs):
            try:
                cid, d = f.result()
                out[cid] = d
            except Exception as e:
                errs += 1
                print(f"[fetch] {type(e).__name__}: {str(e)[:90]}", file=sys.stderr)
    if errs:
        print(f"[fetch] {errs}/{len(cids)} failed", file=sys.stderr)
    return out


# ---------- transcript + index ----------

def messages_of(data, tail=None):
    """User/assistant texts on current branch, oldest first (tail=N -> last N)."""
    mapping = data.get("mapping") or {}
    node_id = data.get("current_node")
    msgs, seen = [], set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        msg = node.get("message")
        if msg:
            role = (msg.get("author") or {}).get("role")
            content = msg.get("content") or {}
            if role in ("user", "assistant") and content.get("content_type") == "text":
                parts = [x for x in content.get("parts", []) if isinstance(x, str)]
                t = "\n".join(parts).strip()
                if t:
                    msgs.append((role, t))
        node_id = node.get("parent")
    if tail is not None:
        msgs = msgs[-tail:]
    msgs.reverse()
    return msgs


def mapping_to_markdown(data):
    md = [f"# {data.get('title') or 'Untitled'}", ""]
    for role, t in messages_of(data):
        md += ["**You**" if role == "user" else "**ChatGPT**", "", t, ""]
    return "\n".join(md)


LIST_TTL = 300      # conversation list cache: 5 min

# OpenAI's conversation endpoint enforces a sliding-window budget (~50 fetches)
# then 429s for a few minutes. Policy: pace body fetches, and on 429 cool off
# globally instead of hammering (hammering extends the block).
# Live settings (Plugins page) win over these module defaults.
PACE = float(os.environ.get("CHATGPT_PACE", "2.5"))   # min seconds between body fetches
COOL = float(os.environ.get("CHATGPT_COOL", "90"))    # cool-off seconds on 429
_last_fetch = [0.0]


def _pace_secs():
    return _setting_float("pace", "CHATGPT_PACE", PACE)


def _cool_secs():
    return _setting_float("cool", "CHATGPT_COOL", COOL)


def _pace():
    p = _pace_secs()
    dt = time.time() - _last_fetch[0]
    if dt < p:
        time.sleep(p - dt)
    _last_fetch[0] = time.time()


def _cool(reason):
    c = _cool_secs()
    print(f"[throttle] {reason} -> cooling off {c:.0f}s", file=sys.stderr)
    time.sleep(c)


def _db():
    db = sqlite3.connect(_paths.INDEX_DB)
    os.chmod(_paths.INDEX_DB, 0o600)
    db.execute("""CREATE TABLE IF NOT EXISTS convs
        (id TEXT PRIMARY KEY, title TEXT, update_time TEXT, tail INTEGER)""")
    try:
        db.execute("ALTER TABLE convs ADD COLUMN tail INTEGER")
    except Exception:
        pass
    db.execute("""CREATE TABLE IF NOT EXISTS bodies
        (conv_id TEXT PRIMARY KEY, body TEXT)""")
    try:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS conv_fts USING fts5(
            title, body, conv_id UNINDEXED, tokenize='unicode61')""")
        have_fts = True
    except Exception:
        have_fts = False
    return db, have_fts


def _list_cache_load():
    try:
        db = sqlite3.connect(_paths.INDEX_DB)
        row = db.execute(
            "SELECT payload, fetched_at FROM list_cache WHERE id = 1").fetchone()
        db.close()
        if row and time.time() - row[1] < LIST_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _list_cache_save(items):
    try:
        slim = [{"id": i["id"], "title": i.get("title"),
                 "update_time": i.get("update_time")} for i in items]
        db = sqlite3.connect(_paths.INDEX_DB)
        db.execute("CREATE TABLE IF NOT EXISTS list_cache "
                   "(id INTEGER PRIMARY KEY, payload TEXT, fetched_at REAL)")
        db.execute("INSERT OR REPLACE INTO list_cache VALUES (1, ?, ?)",
                   (json.dumps(slim), time.time()))
        db.commit(); db.close()
    except Exception:
        pass


def index_conversation(db, have_fts, cid, title, update_time, data, tail=None):
    body = "\n".join(f"{r}: {t}" for r, t in messages_of(data, tail=tail))
    db.execute("""INSERT INTO convs (id, title, update_time, tail) VALUES (?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET title=excluded.title,
        update_time=excluded.update_time, tail=excluded.tail""",
        (cid, title, str(update_time), tail))
    db.execute("INSERT OR REPLACE INTO bodies VALUES (?,?)", (cid, body))
    if have_fts:
        db.execute("DELETE FROM conv_fts WHERE conv_id = ?", (cid,))
        db.execute("INSERT INTO conv_fts (title, body, conv_id) VALUES (?,?,?)",
                   (title or "", body, cid))
    db.commit()


def fts_search(query, limit=10):
    db, have_fts = _db()
    rows = []
    if have_fts:
        q = " OR ".join(f'"{w}"' for w in query.split() if w) or '""'
        try:
            rows = db.execute(
                "SELECT f.conv_id, c.title, c.update_time, "
                "replace(replace(snippet(conv_fts, 1, '>>', '<<', ' … ', 24), char(10), ' '), char(13), ' ') "
                "FROM conv_fts f JOIN convs c ON c.id = f.conv_id "
                "WHERE conv_fts MATCH ? ORDER BY bm25(conv_fts) LIMIT ?",
                (q, limit)).fetchall()
        except Exception:
            rows = []
    if not rows:
        like = f"%{query}%"
        rows = db.execute(
            "SELECT c.id, c.title, c.update_time, "
            "substr(b.body, max(1, instr(lower(b.body), lower(?)) - 60), 180) "
            "FROM bodies b JOIN convs c ON c.id = b.conv_id "
            "WHERE lower(b.body) LIKE lower(?) OR lower(c.title) LIKE lower(?) LIMIT ?",
            (like, like, like, limit)).fetchall()
    db.close()
    return rows


# ---------- commands ----------

def cmd_status(a):
    d = API().session()
    u = d.get("user") or {}
    print(json.dumps({"hasToken": bool(d.get("accessToken")),
                      "email": u.get("email"),
                      "plan": u.get("chatgpt_plan_type") or u.get("plan_type")},
                     indent=2))


def cmd_list(a):
    items = API().conversations(per_page=a.limit or 100)
    if a.limit:
        items = items[: a.limit]
    for it in items:
        t = str(it.get("update_time") or "")[:16]
        print(f"  {it['id']}  {t}  {(it['title'] or '')[:70]}")


# Char budget per markdown page (~10k tokens). Message sizes vary 100x
# (code dumps vs "ok"), so a char cap bounds context better than count alone.
PAGE_CHARS = 40000


def cmd_get(a, as_md=False):
    d = API().conversation(a.cid)
    if not as_md:
        print(json.dumps(d, indent=2))
        return
    # Windowed transcript: newest `limit` messages, `offset` skips that many
    # from the END (offset=50,limit=50 = previous page), hard-capped at
    # `chars` total characters. Walk newest->oldest so the freshest content
    # always survives; the oldest boundary message is the one truncated, and
    # it is re-delivered in full on the previous page (not counted in the
    # next offset), so no text is ever lost between pages.
    msgs = messages_of(d)
    total = len(msgs)
    limit = max(1, min(int(getattr(a, "limit", 0) or 0), 200)) or 50
    offset = max(0, int(getattr(a, "offset", 0) or 0))
    budget = max(2000, int(getattr(a, "chars", 0) or 0)
                 or int(_setting_float("page_chars", "CHATGPT_PAGE_CHARS",
                                       PAGE_CHARS)))
    end = total - offset                      # exclusive, 0-based
    lo = max(0, end - limit)

    page, used, cut = [], 0, None             # cut = (msg_index, dropped_chars)
    for i in range(end - 1, lo - 1, -1):      # newest -> oldest
        role, t = msgs[i]
        cost = len(t) + 40                    # role markers + separators
        room = budget - used
        if cost > room:
            keep = max(0, room - 60)
            cut = (i, len(t) - keep)
            page.append((role, t[:keep], i))
            break
        page.append((role, t, i))
        used += cost
    page.reverse()                            # chronological

    md = [f"# {d.get('title') or 'Untitled'}", ""]
    start_num = page[0][2] + 1
    full = len(page) - (1 if cut else 0)
    next_offset = offset + full               # partial message is re-fetched
    shown_chars = sum(len(t) for _, t, _ in page)
    if cut or offset or end < total:
        md.insert(2, f"*messages {start_num}–{end} of {total}; "
                     f"~{shown_chars // 4:,} tokens"
                     + (f"; last message cut {cut[1]:,} chars" if cut else "")
                     + f"; offset={next_offset} for the previous page*\n")
    for n, (role, t, _) in enumerate(page):
        if cut and n == len(page) - 1:
            t += f"\n\n…[{cut[1]:,} chars truncated — full text on the previous page]"
        md += ["**You**" if role == "user" else "**ChatGPT**", "", t, ""]
    print("\n".join(md))


def cmd_sync(a):
    api, db = API(), None
    db, have_fts = _db()
    idx = api.conversations()
    known = {r[0]: (r[1], r[2]) for r in
             db.execute("SELECT id, update_time, tail FROM convs")}
    tail = a.tail

    def stale(it):
        prev = known.get(it["id"])
        if prev is None:
            return True
        ut, t = prev
        return ut != str(it["update_time"]) or \
            (tail is not None and (t is None or t < tail))

    todo = [i for i in idx if stale(i)]
    print(f"index: {len(idx)} conversations, {len(todo)} new/changed "
          f"(pace={PACE:.1f}s)")
    ok = 0
    for n, it in enumerate(todo, 1):
        try:
            d = api.conversation(it["id"])
        except Exception as e:
            print(f"  [{n}/{len(todo)}] FAIL {str(e)[:60]} {it['title'][:40]}")
            continue
        if not d:
            print(f"  [{n}/{len(todo)}] FAIL {it['title'][:50]}")
            continue
        index_conversation(db, have_fts, it["id"], it["title"], it["update_time"],
                           d, tail=tail)
        ok += 1
        print(f"  [{n}/{len(todo)}] {it['title'][:55]}")
    print(f"synced {ok}/{len(todo)} -> {_paths.INDEX_DB}")
    if ok < len(todo):
        print(f"failed (rerun sync to retry): {len(todo) - ok}")


def cmd_find(a):
    """Hybrid quick search: live titles + index + live grep of recent unsynced.

    Stage timings go to stderr; results to stdout."""
    t0 = time.perf_counter()
    def mark(stage):
        print(f"[find] {stage}: {time.perf_counter() - t0:.1f}s", file=sys.stderr)

    q = a.query.lower()
    words = [w for w in q.split() if w]
    api = API()
    hits = {}          # cid -> (title, where, update_time, snippet)

    idx = api.conversations()                       # 1) live titles
    mark(f"list ({len(idx)} convs)")
    for it in idx:
        if q in (it["title"] or "").lower():
            hits[it["id"]] = (it["title"], "title", it.get("update_time"), "")

    for cid, title, ut, snip in fts_search(a.query, limit=15):   # 2) index
        if cid in hits:
            prev = hits[cid]
            hits[cid] = (title, "title+index", prev[2] or ut, snip)
        else:
            hits[cid] = (title, "index", ut, snip)
    mark(f"index ({len(hits)} hits)")

    if not hits or a.deep:                          # 3) parallel live grep
        db, have_fts = _db()
        known = {r[0] for r in db.execute("SELECT id FROM convs")}
        fresh = [i for i in idx if i["id"] not in known][: a.recent]
        if fresh:
            try:
                docs = fetch_many([i["id"] for i in fresh], token=api.tok,
                                  workers=a.jobs)
            except Exception:
                docs = {}
            mark(f"fetched {len(docs)}/{len(fresh)} unsynced convs")
            for it in fresh:                        # grep + index in main thread
                d = docs.get(it["id"])
                if not d:
                    continue
                body = "\n".join(t for _, t in messages_of(d))
                low = body.lower()
                if any(w in low for w in words):
                    pos = min((low.find(w) for w in words if w in low), default=0)
                    snip = re.sub(r"\s+", " ", body[max(0, pos - 70):pos + 170])
                    hits[it["id"]] = (it["title"], "body-live",
                                      it.get("update_time"), snip)
                index_conversation(db, have_fts, it["id"], it["title"],
                                   it["update_time"], d)
            mark("grepped + indexed")

    if not hits:
        print("no matches")
    for cid, (title, where, ut, snip) in sorted(
            hits.items(), key=lambda kv: kv[1][2] or "", reverse=True):
        when = str(ut or "")[:10]
        print(f"  {when}  {cid}  [{where}]  {title}")
        if snip:
            print(f"      … {snip} …")
    mark("done")


def cmd_search(a):
    rows = fts_search(a.query, limit=a.limit)
    if not rows:
        print("no matches (index only — try `find` for live search)")
    for cid, title, ut, snip in rows:
        print(f"  {str(ut or '')[:10]}  {cid}  {title}")
        if snip:
            print(f"      … {snip} …")


def cmd_export(a):
    out = a.out or _setting("export_dir", "CHATGPT_EXPORT_DIR",
                            os.path.join(_paths.STORAGE, "export"))
    os.makedirs(out, exist_ok=True)
    api = API()
    idx = api.conversations()
    print(f"index: {len(idx)} conversations")
    ok = 0
    for n, it in enumerate(idx, 1):
        try:
            d = api.conversation(it["id"])
        except Exception:
            print(f"  [{n}/{len(idx)}] FAIL {it['title'][:50]}")
            continue
        safe = re.sub(r"[^\w\-]+", "_", (it["title"] or "untitled")[:60])
        with open(os.path.join(out, f"{safe}_{it['id'][:8]}.json"), "w") as f:
            json.dump(d, f)
        with open(os.path.join(out, f"{safe}_{it['id'][:8]}.md"), "w") as f:
            f.write(mapping_to_markdown(d))
        ok += 1
        print(f"  [{n}/{len(idx)}] {it['title'][:60]}")
        time.sleep(0.35)
    with open(os.path.join(out, "_index.json"), "w") as f:
        json.dump(idx, f, indent=2)
    print(f"done: {ok}/{len(idx)} -> {out}")


def import_cookies(export_path):
    """Convert a browser-extension cookies export (JSON list) to the plugin jar.

    Keeps only chatgpt.com/openai.com cookies, int-casts expiry (obscura/CDP
    rejects float), and writes MASTER_JAR with mode 600. Returns a summary.
    """
    with open(export_path) as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise RuntimeError("not a cookies export (expected a JSON list)")
    if not raw:
        raise RuntimeError(
            "cookies export is empty — export cookies while logged in to "
            "chatgpt.com, and check the export includes chatgpt.com")
    samesite = {"lax": "Lax", "strict": "Strict",
                "no_restriction": "None", "unspecified": "Lax"}
    out = []
    for c in raw:
        d = (c.get("domain") or "").lstrip(".")
        if not (d == "chatgpt.com" or d.endswith(".chatgpt.com")
                or d == "openai.com" or d.endswith(".openai.com")):
            continue
        exp = c.get("expirationDate")
        out.append({
            "name": c["name"], "value": c["value"], "domain": c["domain"],
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": samesite.get(c.get("sameSite", "unspecified"), "Lax"),
            "expires": int(exp) if exp is not None else None,
        })
    if not any("__Secure-next-auth.session-token" in c["name"] for c in out):
        found = sorted({c["name"] for c in out})[:8]
        raise RuntimeError(
            "no ChatGPT session token in export — export cookies while "
            "LOGGED IN to chatgpt.com (needs "
            "'__Secure-next-auth.session-token'). "
            f"Scoped cookies found: {found or 'none'}")
    os.makedirs(_paths.STORAGE, mode=0o700, exist_ok=True)
    jar = _paths.MASTER_JAR
    with open(jar, "w") as f:
        json.dump(out, f, indent=2)
    os.chmod(jar, 0o600)
    return {"imported": len(out), "jar": jar,
            "chunks": sorted(c["name"] for c in out
                             if "__Secure-next-auth.session-token" in c["name"]),
            "cf_clearance": any(c["name"] == "cf_clearance" for c in out)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("list"); p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("get"); p.add_argument("cid")
    p = sub.add_parser("get-md"); p.add_argument("cid")
    for _sp in (sub.choices["get-md"],):
        _sp.add_argument("--limit", type=int, default=50,
                         help="max messages returned (newest window; 1-200)")
        _sp.add_argument("--offset", type=int, default=0,
                         help="skip this many newest messages (page backwards)")
        _sp.add_argument("--chars", type=int, default=0,
                         help=f"max chars per page (default {PAGE_CHARS}, ~10k tokens)")
    p = sub.add_parser("sync"); p.add_argument("--tail", type=int, default=None)
    p.add_argument("--jobs", type=int, default=6)
    p = sub.add_parser("find"); p.add_argument("query")
    p.add_argument("--recent", type=int, default=10)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--deep", action="store_true",
                   help="grep unsynced bodies even when title/index already hit")
    p = sub.add_parser("search"); p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("export"); p.add_argument("--out")
    p = sub.add_parser("import-cookies"); p.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "import-cookies":
        print(json.dumps(import_cookies(a.path), indent=2))
    else:
        {"status": cmd_status, "list": cmd_list,
         "get": lambda x: cmd_get(x, False), "get-md": lambda x: cmd_get(x, True),
         "sync": cmd_sync, "find": cmd_find, "search": cmd_search,
         "export": cmd_export}[a.cmd](a)
