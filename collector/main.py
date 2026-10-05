"""AOR Radar: daily collector.

Pipeline: fetch (RSS + Brave search) -> dedupe -> Claude classify/extract -> merge into
docs/openings.json -> email digest with only the NEW items.
"""
import datetime as dt
import hashlib
import json
import os
import re
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
import yaml
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.yaml"
OPENINGS = ROOT / "docs" / "openings.json"
SEEN = ROOT / "data" / "seen.json"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
BATCH = 15
KEEP_DAYS = 90
UA = {"User-Agent": "Mozilla/5.0 (AOR-Radar; +https://mindrootlabs.github.io)"}
TODAY = dt.date.today().isoformat()


def log(msg):
    print(msg, flush=True)


def norm_url(u):
    p = urlsplit(u.strip())
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", ""))


def item_id(u):
    return hashlib.sha1(norm_url(u).encode()).hexdigest()[:16]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------- fetch ----------
def fetch_rss(cfg):
    rx = re.compile(cfg.get("rss_prefilter_regex", "."), re.I)
    out = []
    for feed in cfg.get("rss_feeds", []):
        try:
            resp = requests.get(feed["url"], headers=UA, timeout=20)
            parsed = feedparser.parse(resp.content)
            n = 0
            for e in parsed.entries[:60]:
                text = f"{e.get('title','')} {e.get('summary','')}"
                if rx.search(text) and e.get("link"):
                    out.append({"title": e.get("title", ""), "url": e["link"],
                                "snippet": re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:600],
                                "source": feed["name"]})
                    n += 1
            log(f"[rss] {feed['name']}: {len(parsed.entries)} entries, {n} matched")
        except Exception as ex:
            log(f"[rss] {feed['name']} FAILED: {ex}")
    return out


def fetch_search(cfg):
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        log("[search] BRAVE_API_KEY not set, skipping web search")
        return []
    out = []
    for q in cfg.get("search_queries", []):
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": q, "count": 20, "freshness": "pw", "country": "in"},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                timeout=20)
            r.raise_for_status()
            res = r.json().get("web", {}).get("results", [])
            for x in res:
                out.append({"title": x.get("title", ""), "url": x["url"],
                            "snippet": re.sub(r"<[^>]+>", "", x.get("description", ""))[:600],
                            "source": urlsplit(x["url"]).netloc.replace("www.", "")})
            log(f"[search] {q[:50]!r}: {len(res)} results")
        except Exception as ex:
            log(f"[search] {q[:50]!r} FAILED: {ex}")
    return out


# ---------- classify ----------
PROMPT = """You screen web results for an Advocate-on-Record (AOR) at the Supreme Court of India who is looking for RETAINER or PANEL opportunities.

For each numbered item decide if it is a genuine, currently actionable opportunity for an AOR, such as:
- a vacancy / retainership for an AOR or Supreme Court counsel (law firm, company, PSU, bank, NGO)
- an empanelment / panel-counsel invitation that includes the Supreme Court
- a tender or expression of interest for legal services before the Supreme Court
NOT relevant: news stories, judgments, general articles, junior-associate or fresher jobs, internships, expired notices, aggregator category pages with no specific posting.

Return ONLY a JSON array, one object per item, in order:
{"i": <number>, "relevant": true|false, "title": "<clean title>", "org": "<organisation or null>", "location": "<city/state or null>", "deadline": "<YYYY-MM-DD or null>", "type": "panel|job|tender|other", "summary": "<one sentence, max 25 words>"}
Use only what the text supports; use null if unknown. Today is """ + TODAY + "."


def parse_json_array(txt):
    """Find the model's JSON array even if it adds prose, code fences or '[0]'-style labels."""
    dec = json.JSONDecoder()
    empty_found = False
    for m in re.finditer(r"\[", txt):
        try:
            val, _ = dec.raw_decode(txt[m.start():])
        except ValueError:
            continue
        if not isinstance(val, list):
            continue
        if not val:
            empty_found = True
        elif all(isinstance(x, dict) and "i" in x for x in val):
            return val
    if empty_found:
        return []
    raise ValueError("no JSON array of results found")


def classify(client, items):
    results = []
    for s in range(0, len(items), BATCH):
        chunk = items[s:s + BATCH]
        body = "\n\n".join(
            f"Item {i}: {it['title']}\nURL: {it['url']}\nSource: {it['source']}\n{it['snippet']}"
            for i, it in enumerate(chunk))
        txt = ""
        try:
            msg = client.messages.create(model=MODEL, max_tokens=3000,
                                         messages=[{"role": "user", "content": PROMPT + "\n\n" + body}])
            txt = msg.content[0].text
            arr = parse_json_array(txt)
            kept = set()
            for a in arr:
                idx = a.get("i")
                if isinstance(idx, int) and 0 <= idx < len(chunk) and a.get("relevant"):
                    kept.add(idx)
                    it = chunk[idx]
                    results.append({
                        "id": item_id(it["url"]), "title": a.get("title") or it["title"],
                        "org": a.get("org"), "location": a.get("location"),
                        "deadline": a.get("deadline"), "type": a.get("type") or "other",
                        "summary": a.get("summary"), "url": it["url"],
                        "source": it["source"], "first_seen": TODAY})
            log(f"[classify] batch {s}: {len(kept)} relevant of {len(chunk)}")
            for j, it in enumerate(chunk):
                if j not in kept:
                    log(f"   - rejected: {it['title'][:90]} ({it['source']})")
        except Exception as ex:
            log(f"[classify] batch {s} FAILED: {ex} | response head: {txt[:200]!r}")
            # do not mark these as seen so they get retried tomorrow
            for it in chunk:
                it["_failed"] = True
    return results


# ---------- notify ----------
def digest_text(new, total):
    if not new:
        return f"AOR Radar {TODAY}: no new openings today. {total} active on the dashboard."
    lines = [f"AOR Radar {TODAY}: {len(new)} new opening(s)\n"]
    for n in new:
        meta = " | ".join(x for x in [n.get("org"), n.get("location"),
                                      f"deadline {n['deadline']}" if n.get("deadline") else None] if x)
        lines.append(f"- {n['title']}\n  {meta}\n  {n.get('summary') or ''}\n  Apply/view: {n['url']}\n")
    dash = os.environ.get("DASHBOARD_URL")
    if dash:
        lines.append(f"Dashboard: {dash}")
    return "\n".join(lines)


def send_email(subject, text):
    u, p, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("EMAIL_TO")
    if not (u and p and to):
        return False
    m = MIMEMultipart()
    m["From"], m["To"], m["Subject"] = u, to, subject
    m.attach(MIMEText(text, "plain", "utf-8"))
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(u, p)
        s.sendmail(u, [x.strip() for x in to.split(",")], m.as_string())
    return True


def notify(new, total):
    text = digest_text(new, total)
    subject = f"AOR Radar: {len(new)} new opening(s)" if new else "AOR Radar: nothing new today"
    try:
        log(f"[notify] email: {'sent' if send_email(subject, text) else 'not configured (set SMTP_USER, SMTP_PASS, EMAIL_TO)'}")
    except Exception as ex:
        log(f"[notify] email FAILED: {ex}")


# ---------- main ----------
def main():
    dry = "--dry-run" in sys.argv
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data = load_json(OPENINGS, {"updated": None, "items": []})
    seen = set(load_json(SEEN, []))

    raw = fetch_rss(cfg) + fetch_search(cfg)
    uniq, ids = [], set()
    for it in raw:
        i = item_id(it["url"])
        if i in seen or i in ids:
            continue
        ids.add(i)
        uniq.append(it)
    log(f"[main] {len(raw)} fetched, {len(uniq)} unseen")

    if not uniq:
        new = []
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        log("[main] ANTHROPIC_API_KEY missing, cannot classify")
        sys.exit(1)
    else:
        new = classify(Anthropic(), uniq)
        for it in uniq:
            if not it.get("_failed"):
                seen.add(item_id(it["url"]))

    # merge, drop expired / stale
    existing = {x["id"]: x for x in data.get("items", [])}
    for n in new:
        existing.setdefault(n["id"], n)
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = [x for x in existing.values()
             if x["first_seen"] >= cutoff and not (x.get("deadline") and x["deadline"] < TODAY)]
    items.sort(key=lambda x: (x["first_seen"], x["title"]), reverse=True)
    log(f"[main] {len(new)} new relevant, {len(items)} active total")

    if dry:
        log(digest_text(new, len(items)))
        return
    OPENINGS.parent.mkdir(exist_ok=True)
    SEEN.parent.mkdir(exist_ok=True)
    OPENINGS.write_text(json.dumps({"updated": dt.datetime.now(dt.timezone.utc).isoformat(),
                                    "items": items}, indent=1, ensure_ascii=False), encoding="utf-8")
    SEEN.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    notify(new, len(items))


if __name__ == "__main__":
    main()