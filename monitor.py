#!/usr/bin/env python3
"""Career Route official-source monitor.
Fetches each page in updates.json, hashes its visible text, and flags a
change for HUMAN REVIEW when the hash differs from the last run.
Standard library only."""
import hashlib, json, os, re, sys, urllib.request
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

FEED = "updates.json"
STATE = "state.json"
UA = "Mozilla/5.0 (compatible; CareerRouteMonitor/1.0; personal use)"
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


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    p = Text()
    p.feed(html)
    text = re.sub(r"\s+", " ", " ".join(p.parts)).strip()
    return re.sub(r"\s+", " ", p.title).strip(), text


def main():
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    force = os.environ.get("FORCE_ALL") == "1"
    feed = json.load(open(FEED, encoding="utf-8"))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    ok = bad = 0
    failures = []
    events = feed.get("events", [])

    for s in feed["sources"]:
        sid = s["id"]
        prev = state.get(sid, {})
        due = force or s.get("priority") == "critical" or now.weekday() == 0 or not prev.get("hash")
        if not due:
            continue
        try:
            title, text = fetch(s["url"])
            if len(text) < 200:
                raise ValueError("page returned almost no text (blocked or empty)")
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            s["title"], s["checkedAt"] = title, stamp
            if prev.get("hash") and prev["hash"] != h:
                s["status"] = "changed"
                events.append({
                    "id": sid, "country": s["country"], "title": s["name"],
                    "url": s["url"], "detectedAt": stamp, "status": "needs_human_review",
                    "message": "Page text changed since the last check. This may be minor "
                               "(dates, wording). Open the source and confirm before acting."})
            else:
                s["status"] = "ok"
            state[sid] = {"hash": h, "checkedAt": stamp}
            ok += 1
        except Exception as e:
            s["status"], s["checkedAt"] = "check_failed", stamp
            failures.append({"id": sid, "name": s["name"], "url": s["url"], "error": str(e)[:150]})
            bad += 1

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
    print(f"ok={ok} failed={bad}")


if __name__ == "__main__":
    sys.exit(main())
