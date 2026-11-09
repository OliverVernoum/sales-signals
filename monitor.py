"""Story monitor / sales-intelligence checker.

Each run:
  1. reads everyone's alert settings from Supabase and the ready-made sources
     and sales triggers from docs/presets.json
  2. fetches every followed site / feed / YouTube channel once
  3. matches new stories against each person's triggers and keywords and
     saves matches as "findings"
  4. emails each person their unsent findings, now / daily / weekly as chosen

Usage:
    python monitor.py            # normal run
    python monitor.py --dry-run  # print what would happen; send/save nothing
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import smtplib
import socket
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
PRESETS_FILE = ROOT / "docs" / "presets.json"
SEEN_PER_SITE = 500          # remembered story ids per site
MAX_BYTES = 5_000_000        # ignore anything bigger than 5 MB
KEEP_FINDINGS_DAYS = 90
LOCAL_TZ = ZoneInfo("Europe/London")
DIGEST_HOUR = 8              # daily/weekly digests go out from 8am UK time
RELEVANCE = {"Very high": 3, "High": 2, "Medium": 1}
RELEVANCE_NAME = {3: "Very high", 2: "High", 1: "Medium", 0: "Your keywords"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; story-monitor/1.0)"}


# ---------- Supabase ----------

def sb(method, path, prefer="return=minimal", **kw):
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {"apikey": key, "Content-Type": "application/json", "Prefer": prefer}
    if not key.startswith("sb_"):  # legacy JWT keys also go in Authorization
        headers["Authorization"] = f"Bearer {key}"
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path
    r = requests.request(method, url, headers=headers, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


class Store:
    """All database access, so tests can swap in a fake."""

    def alerts(self):
        return sb("GET", "alerts?select=*&paused=eq.false")

    def add_findings(self, rows):
        if rows:
            sb("POST", "findings?on_conflict=user_id,story_id",
               prefer="return=minimal,resolution=ignore-duplicates", json=rows)

    def unsent(self):
        return sb("GET", "findings?select=*&sent=eq.false&order=relevance.desc,found_at.desc")

    def mark_sent(self, user_id, ids):
        sb("PATCH", f"findings?id=in.({','.join(map(str, ids))})", json={"sent": True})
        sb("PATCH", f"alerts?user_id=eq.{user_id}",
           json={"last_digest_at": datetime.now(timezone.utc).isoformat()})

    def prune(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_FINDINGS_DAYS)).isoformat()
        sb("DELETE", f"findings?found_at=lt.{cutoff}")


# ---------- safe fetching ----------

def _check_public(url):
    """Refuse non-http(s) URLs and hosts that resolve to private/internal IPs,
    so people can't make the checker fetch internal addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("not an http(s) URL")
    for info in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("host is not a public address")


def fetch(url):
    """GET a URL, following up to 5 redirects, checking every hop."""
    for _ in range(6):
        _check_public(url)
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=False, stream=True)
        if r.is_redirect:
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        r.raise_for_status()
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError("response too large")
        return url, body.decode(r.encoding or "utf-8", errors="replace")
    raise ValueError("too many redirects")


# ---------- parsing ----------

def _text(el, *names):
    """First non-empty child text among tag names (namespace-agnostic)."""
    for child in el:
        tag = child.tag.split("}")[-1]
        if tag in names:
            if tag == "link" and child.get("href"):  # Atom
                return child.get("href")
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def looks_like_feed(text):
    head = text.lstrip()[:500].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


def parse_feed(text):
    root = ET.fromstring(text.encode())
    items = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("item", "entry"):
            summary = _text(el, "description", "summary", "content")
            if not summary:  # YouTube puts the description in media:group/media:description
                for d in el.iter():
                    if d.tag.endswith("}description") and d.text:
                        summary = d.text
                        break
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:1000]
            items.append({"title": _text(el, "title"),
                          "link": _text(el, "link", "guid", "id"),
                          "summary": summary})
    return items


def find_feed_link(html, base_url):
    """RSS/Atom auto-discovery: <link rel="alternate" type="application/rss+xml">."""
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])).lower()
        if "alternate" in rel and link.get("type", "").lower() in (
                "application/rss+xml", "application/atom+xml"):
            href = urljoin(base_url, link["href"])
            if "/comments/" not in href:  # skip WordPress comment feeds
                return href
    return None


def parse_page(html, base_url):
    """Fallback scraper: headline links inside articles / h2 / h3 / cards."""
    soup = BeautifulSoup(html, "html.parser")
    items, links = [], set()
    host = urlparse(base_url).hostname
    for a in soup.select("article a[href], h2 a[href], h3 a[href], h4 a[href], a[href] h2, a[href] h3"):
        if a.name != "a":
            a = a.find_parent("a")
        title = a.get_text(" ", strip=True)
        link = urljoin(base_url, a["href"]).split("#")[0]
        if len(title) >= 20 and link not in links and urlparse(link).hostname == host:
            links.add(link)
            items.append({"title": title, "link": link, "summary": ""})
    return items


def get_stories(url):
    final_url, text = fetch(url)
    if looks_like_feed(text):
        return parse_feed(text)
    feed = find_feed_link(text, final_url)
    if feed:
        try:
            _, feed_text = fetch(feed)
            stories = parse_feed(feed_text)
            if stories:
                return stories
        except Exception:
            pass  # fall back to scraping the page
    return parse_page(text, final_url)


# ---------- matching ----------

def term_regex(term):
    """'retire*' matches retire/retired/retirement; ALL-CAPS terms (CFO, US, AI)
    are case-sensitive so 'US' doesn't match 'us'."""
    term = term.strip()
    star = term.endswith("*")
    body = r"\s+".join(re.escape(w) for w in term.rstrip("*").split())
    pat = r"\b" + body + (r"\w*" if star else r"\b")
    letters = re.sub(r"[^A-Za-z]", "", term)
    if len(letters) >= 2 and letters.isupper():
        return pat
    return "(?i:" + pat + ")"


def build_pattern(words):
    parts = [term_regex(w) for w in (words or []) if w.strip()]
    return re.compile("|".join(parts)) if parts else None


class Trigger:
    def __init__(self, spec):
        self.id, self.name, self.action = spec["id"], spec["name"], spec["action"]
        self.relevance = RELEVANCE.get(spec["relevance"], 1)
        self.groups = [build_pattern(g) for g in spec["all"]]
        self.kinds = set(spec.get("kinds", []))

    def matches(self, text, kind):
        return kind in self.kinds or all(g and g.search(text) for g in self.groups)


def load_presets():
    data = json.loads(PRESETS_FILE.read_text())
    sources = {s["id"]: s for s in data["sources"]}
    triggers = {t["id"]: Trigger(t) for t in data["triggers"]}
    return sources, triggers


def story_id(story):
    return hashlib.sha1((story["link"] or story["title"]).encode()).hexdigest()[:16]


def watched_urls(alert, sources):
    """{url: (source name, kind)} for one person's alert settings."""
    out = {}
    for sid in alert.get("sources") or []:
        src = sources.get(sid)
        for u in (src["urls"] if src else []):
            out[u["url"]] = (src["name"], u["kind"])
    for u in alert.get("sites") or []:
        u = u.strip()
        out.setdefault(u, (urlparse(u).hostname or u, "custom"))
    return out


def evaluate(alert, story, kind, triggers):
    """Return (trigger ids, keyword, relevance) or None if nothing matches."""
    text = f"{story['title']} {story['summary']}"
    exclude = build_pattern(alert.get("exclude"))
    if exclude and exclude.search(text):
        return None
    hit_triggers = [t for tid in alert.get("triggers") or []
                    if (t := triggers.get(tid)) and t.matches(text, kind)]
    kw_pat = build_pattern(alert.get("keywords"))
    kw = kw_pat.search(text) if kw_pat else None
    if not hit_triggers and not kw:
        return None
    hit_triggers.sort(key=lambda t: -t.relevance)
    relevance = hit_triggers[0].relevance if hit_triggers else 0
    return [t.id for t in hit_triggers], (kw.group(0) if kw else None), relevance


# ---------- email ----------

class Mailer:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.conn = None

    def send(self, to, subject, plain, html):
        if self.dry_run:
            print(f"  [dry run] email to {to}: {subject}")
            return
        if self.conn is None:
            self.conn = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=30)
            self.conn.starttls()
            self.conn.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("EMAIL_FROM", os.environ["SMTP_USER"])
        msg["To"] = to
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
        self.conn.send_message(msg)

    def close(self):
        if self.conn:
            self.conn.quit()


def build_digest(alert, findings, triggers):
    site = os.environ["SITE_URL"].rstrip("/")
    unsub = f"{site}/?unsubscribe={alert['token']}"
    by_level = defaultdict(list)
    for f in findings:
        by_level[f["relevance"]].append(f)

    plain, html = [], []
    for level in sorted(by_level, reverse=True):
        heading = RELEVANCE_NAME[level] + (" relevance" if level else "")
        plain.append(f"== {heading} ==")
        html.append(f"<h3 style=\"margin:20px 0 8px;font:600 15px sans-serif\">{escape(heading)}</h3><ul style=\"padding-left:18px\">")
        for f in by_level[level]:
            names = [triggers[t].name for t in f["triggers"] if t in triggers]
            actions = [triggers[t].action for t in f["triggers"] if t in triggers]
            why = "; ".join(names) or f"Matched “{f['keyword']}”"
            plain.append(f"- {f['title']}\n  {f['source']} · {why}\n"
                         + (f"  Action: {actions[0]}\n" if actions else "") + f"  {f['link']}")
            html.append(
                f"<li style=\"margin-bottom:10px\"><a href=\"{escape(f['link'])}\">{escape(f['title'])}</a><br>"
                f"<small>{escape(f['source'])} · <b>{escape(why)}</b>"
                + (f"<br>Suggested action: {escape(actions[0])}" if actions else "")
                + "</small></li>")
        html.append("</ul>")

    n = len(findings)
    top = RELEVANCE_NAME[max(f["relevance"] for f in findings)]
    subject = f"{n} new sales signal{'s' if n != 1 else ''}" + (f" ({top} relevance)" if top != "Your keywords" else "")
    plain.append(f"\nAll findings & settings: {site}/\nUnsubscribe: {unsub}")
    html.append(f"<p><small><a href=\"{escape(site)}/\">All findings &amp; settings</a> · "
                f"<a href=\"{escape(unsub)}\">Unsubscribe</a></small></p>")
    return subject, "\n".join(plain), "".join(html)


def digest_due(alert, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    freq = alert.get("frequency") or "instant"
    if freq == "instant":
        return True
    last = alert.get("last_digest_at")
    last = datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(LOCAL_TZ) if last else None
    today_slot = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if freq == "weekly":
        today_slot -= timedelta(days=now.weekday())  # this week's Monday
    return now >= today_slot and (last is None or last < today_slot)


# ---------- main ----------

def load_seen():
    return json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}


def run(store, seen, mailer, dry_run=False, now=None):
    sources, triggers = load_presets()
    alerts = store.alerts()
    per_alert = {a["user_id"]: watched_urls(a, sources) for a in alerts}

    # 1. Fetch each URL once, however many people follow it
    all_urls = sorted({u for urls in per_alert.values() for u in urls})
    new_stories = {}
    for url in all_urls:
        try:
            stories = get_stories(url)
        except Exception as e:  # one broken site shouldn't stop the rest
            print(f"!! {url}: {e}", file=sys.stderr)
            if url in seen:
                new_stories[url] = []
            continue
        first_time = url not in seen
        known = set(seen.get(url, []))
        fresh = [st for st in stories if story_id(st) not in known]
        # First time we see a URL, just record what's there (no flood of old stories)
        new_stories[url] = [] if first_time else fresh
        seen[url] = (seen.get(url, []) + [story_id(st) for st in fresh])[-SEEN_PER_SITE:]
        print(f"{url}: {len(stories)} stories, {len(fresh)} new{' (first check)' if first_time else ''}")
    seen = {u: ids for u, ids in seen.items() if u in new_stories}

    # 2. Match new stories against each person's triggers and keywords
    rows = []
    for alert in alerts:
        done = set()
        for url, (source, kind) in per_alert[alert["user_id"]].items():
            for st in new_stories.get(url, []):
                sid = story_id(st)
                if sid in done:
                    continue
                result = evaluate(alert, st, kind, triggers)
                if result:
                    done.add(sid)
                    tids, kw, rel = result
                    rows.append({"user_id": alert["user_id"], "story_id": sid, "title": st["title"][:300],
                                 "link": st["link"], "source": source, "triggers": tids,
                                 "keyword": kw, "relevance": rel})
    for r in rows:
        print(f"  FINDING [{RELEVANCE_NAME[r['relevance']]}] {r['source']}: {r['title']} {r['triggers'] or r['keyword']}")
    if not dry_run:
        store.add_findings(rows)
        store.prune()

    # 3. Send digests that are due
    unsent = rows if dry_run else store.unsent()
    by_user = defaultdict(list)
    for f in unsent:
        by_user[f["user_id"]].append(f)
    sent = 0
    for alert in alerts:
        findings = by_user.get(alert["user_id"])
        if not findings or not digest_due(alert, now):
            continue
        findings.sort(key=lambda f: -f["relevance"])
        try:
            mailer.send(alert["email"], *build_digest(alert, findings, triggers))
            if not dry_run:
                store.mark_sent(alert["user_id"], [f["id"] for f in findings])
            sent += 1
        except Exception as e:
            print(f"!! digest for user {alert['user_id']}: {e}", file=sys.stderr)

    print(f"{len(alerts)} active alert(s), {len(rows)} new finding(s), {sent} digest(s) sent.")
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    mailer = Mailer(args.dry_run)
    try:
        seen = run(Store(), load_seen(), mailer, args.dry_run)
    finally:
        mailer.close()
    if not args.dry_run:
        SEEN_FILE.write_text(json.dumps(seen, indent=0))


if __name__ == "__main__":
    main()
