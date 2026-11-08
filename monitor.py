#!/usr/bin/env python3
"""Career Route official-source monitor.
Fetches each page in updates.json, hashes its visible text, and flags a
change for HUMAN REVIEW when the hash differs from the last run.
Standard library only."""
import hashlib, json, os, re, shutil, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

FEED = "updates.json"
# These sites cut off automated requests from GitHub's servers (confirmed 30 Oct 2026),
# so they are listed as "check by hand" instead of waiting minutes for a timeout.
# Set the environment variable TRY_BLOCKED=1 to try them again.
TEXTS = {}  # page text from this run, used for "what changed" and snapshots


def sentences(t):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if len(x.strip()) > 25]


def diff_text(old, new):
    o, n = sentences(old), sentences(new)
    so, sn = set(o), set(n)
    return [x[:220] for x in n if x not in so][:4], [x[:220] for x in o if x not in sn][:3]


def update_costs(events, stamp):
    """Read fee amounts from official pages where a rule exists. Never invent a number:
    if a page cannot be read the old amount stays and is marked as an estimate."""
    try:
        c = json.load(open("costs.json", encoding="utf-8"))
    except Exception:
        return
    names = {"nz": "New Zealand", "ca": "Canada", "au": "Australia"}
    day = stamp[:10]
    for cid, it in c["items"].items():
        r = it.get("rule")
        if not r:
            continue
        try:
            _, text = fetch(it["url"])
            m = re.search(r["re"], text, re.I | re.S)
            if not m:
                raise ValueError("fee not found on page")
            val = float(m.group(1).replace(",", ""))
            if val != it.get("amount"):
                events.append({"id": "fee_" + cid, "kind": "fee", "country": names.get(cid[:2], ""),
                    "title": it["label"], "url": it["url"], "detectedAt": stamp, "status": "needs_human_review",
                    "message": "Fee changed from %s %s to %s %s. Your budget was updated unless you typed your own amount." % (
                        it["cur"], f"{it.get('amount'):,.0f}", it["cur"], f"{val:,.0f}")})
                it["previous"] = it.get("amount")
                it["amount"] = val
            it["status"], it["checked"] = "verified", day
        except Exception:
            if it.get("status") == "verified":
                it["status"] = "estimate"  # keep the last amount we read, but say it is not fresh
    json.dump(c, open("costs.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)


MANUAL = {"ca_language", "ca_fst", "ca_fees", "au_tra"}
STATE = "state.json"
SKIP = {"script", "style", "noscript", "svg", "head"}


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def get_html(url):
    last = None
    for _ in range(1):  # plain Python request
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            time.sleep(3)
    if shutil.which("curl"):  # some sites only answer curl-style connections
        for mode in (["--http1.1"],):  # HTTP/1.1 only
            cmd = ["curl", "-sSL", "--compressed", "--max-time", "25"] + mode
            for k, v in HEADERS.items():
                cmd += ["-H", f"{k}: {v}"]
            p = subprocess.run(cmd + [url], capture_output=True, timeout=40)
            if p.returncode == 0 and p.stdout:
                return p.stdout.decode("utf-8", errors="replace")
            last = f"{last}; curl{' '.join(mode)} exit {p.returncode}"
    raise RuntimeError(str(last))


def fetch(url):
    html = get_html(url)
    p = Text()
    p.feed(html)
    text = re.sub(r"\s+", " ", " ".join(p.parts)).strip()
    return re.sub(r"\s+", " ", p.title).strip(), text


def career_sources(feed):
    """Add each career's regulator pages (from careers.json) as weekly sources."""
    try:
        cd = json.load(open("careers.json", encoding="utf-8"))
    except Exception:
        return
    names = {"NZ": "New Zealand", "CA": "Canada", "AU": "Australia"}
    have = {s["url"]: s for s in feed["sources"] if not s["id"].startswith("reg_")}
    found = {}
    for c in cd.get("careers", []):
        for co, r in c["reg"].items():
            for x in r.get("assess", []) + r.get("licence", []):
                if not x.get("u") or x["u"] in have:
                    continue
                e = found.setdefault(x["u"], {"id": "reg_" + hashlib.sha1(x["u"].encode()).hexdigest()[:8],
                    "country": names[co], "name": x["n"], "url": x["u"], "priority": "weekly",
                    "careers": [], "checkedAt": None, "status": "not_checked", "title": ""})
                if c["id"] not in e["careers"]:
                    e["careers"].append(c["id"])
    old = {s["id"]: s for s in feed["sources"] if s["id"].startswith("reg_")}
    keep = [s for s in feed["sources"] if not s["id"].startswith("reg_")]
    for e in found.values():
        o = old.get(e["id"])
        if o:
            o["careers"] = e["careers"]
            o["name"], o["country"] = e["name"], e["country"]
            keep.append(o)
        else:
            keep.append(e)
    feed["sources"] = keep


def check_one(s, prev, force_try):
    if s["id"] in MANUAL and not force_try:
        return s, "manual", None, None, None
    try:
        title, text = fetch(s["url"])
        if len(text) < 200:
            return s, "empty", None, None, "page returned almost no text (blocked or empty)"
        TEXTS[s["id"]] = text
        return s, "ok", title, hashlib.sha256(text.encode("utf-8")).hexdigest(), None
    except Exception as e:
        return s, "fail", None, None, str(e)[:150]


def render_empty(results):
    """Pages that came back with almost no text are often built by JavaScript.
    Open them in a real headless browser (if Playwright is installed) and read the text."""
    idx = [i for i, r in enumerate(results) if r[1] == "empty"]
    if not idx:
        return results
    results = list(results)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch()
            for i in idx:
                s = results[i][0]
                try:
                    pg = br.new_page(user_agent=HEADERS["User-Agent"])
                    pg.goto(s["url"], wait_until="networkidle", timeout=45000)
                    pg.wait_for_timeout(1500)
                    text = re.sub(r"\s+", " ", pg.inner_text("body")).strip()
                    title = pg.title()
                    pg.close()
                    if len(text) >= 200:
                        TEXTS[s["id"]] = text
                        results[i] = (s, "ok", title, hashlib.sha256(text.encode("utf-8")).hexdigest(), None)
                    else:
                        results[i] = (s, "fail", None, None, "page is empty even in a browser (probably blocked)")
                except Exception as e:
                    results[i] = (s, "fail", None, None, "browser: " + str(e)[:100])
            br.close()
    except Exception as e:
        for i in idx:
            results[i] = (results[i][0], "fail", None, None, "page returned almost no text (blocked or empty)")
    return results


def main():
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    monday = now.weekday() == 0
    force = os.environ.get("FORCE_ALL") == "1"
    force_try = os.environ.get("TRY_BLOCKED") == "1"
    feed = json.load(open(FEED, encoding="utf-8"))
    career_sources(feed)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    events = feed.get("events", [])
    failures, ok, bad, todo = [], 0, 0, []
    for s in feed["sources"]:
        prev = state.get(s["id"], {})
        due = force or s.get("priority") == "critical" or monday or not prev.get("hash")
        if not due:
            continue
        if prev.get("fails", 0) >= 3 and not monday and not force:  # keep failing pages for Mondays
            s["status"], s["checkedAt"] = "manual_check", stamp
            failures.append({"id": s["id"], "name": s["name"], "url": s["url"],
                             "error": "Could not be checked automatically. Open it and check by hand about once a week."})
            bad += 1
            continue
        todo.append((s, prev))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda sp: check_one(sp[0], sp[1], force_try), todo))
    results = render_empty(results)
    for (s, prev), (_, kind, title, h, err) in zip(todo, results):
        sid = s["id"]
        s["checkedAt"] = stamp
        if kind == "manual":
            s["status"] = "manual_check"
            failures.append({"id": sid, "name": s["name"], "url": s["url"],
                             "error": "Blocks automatic checks. Open it and check by hand about once a week."})
            bad += 1
        elif kind == "ok":
            s["title"] = title
            snap = os.path.join("snap", sid + ".txt")
            old = open(snap, encoding="utf-8").read() if os.path.exists(snap) else ""
            if prev.get("hash") and prev["hash"] != h:
                s["status"] = "changed"
                added, removed = diff_text(old, TEXTS.get(sid, "")[:30000]) if old else ([], [])
                ev = {"kind": "change", "added": added, "removed": removed, "id": sid, "country": s["country"], "title": s["name"], "url": s["url"],
                      "detectedAt": stamp, "status": "needs_human_review",
                      "message": "Page text changed since the last check. This may be minor (dates, wording). Open the source and confirm before acting."}
                if s.get("careers"):
                    ev["careers"] = s["careers"]
                events.append(ev)
            else:
                s["status"] = "ok"
            os.makedirs("snap", exist_ok=True)
            open(snap, "w", encoding="utf-8").write(TEXTS.get(sid, "")[:30000])
            state[sid] = {"hash": h, "checkedAt": stamp, "fails": 0}
            ok += 1
        else:
            s["status"] = "check_failed"
            st = state.setdefault(sid, {})
            st["fails"] = st.get("fails", 0) + 1
            failures.append({"id": sid, "name": s["name"], "url": s["url"], "error": err})
            bad += 1
    update_costs(events, stamp)
    cutoff = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = events[-50:]
    feed.update({
        "generatedAt": stamp, "monitoringMode": "scheduled", "events": events,
        "failures": failures, "checksSucceeded": ok, "checksFailed": bad,
        "newChangesDetected": sum(1 for e in events if e.get("detectedAt", "") >= cutoff),
        "importantDisclaimer": "Scheduled checks detect page-text changes only. A detected change "
                               "is not a verified policy change; always confirm requirements and "
                               "fees on the linked official source."})
    json.dump(feed, open(FEED, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(state, open(STATE, "w"), indent=2)
    print(f"ok={ok} failed={bad} sources={len(feed['sources'])}")


if __name__ == "__main__":
    sys.exit(main())
