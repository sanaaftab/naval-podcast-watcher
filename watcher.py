#!/usr/bin/env python3
"""
Naval podcast watcher.

Polls the nav.al RSS feed, detects new podcast episodes (dedup by <guid>),
fetches the episode transcript, summarises it with the Anthropic API in a
fixed format, and emails the summary. Also emails an alert if the feed or the
check itself stops working.

Emails are sent ONLY when:
  * a new podcast episode has been summarised, or
  * something breaks (feed down, parse error, API error, transcript missing...).

State (last-seen guid, alert throttle, heartbeat) lives under ./state and is
committed back to the repo by the GitHub Actions workflow, so it survives
between runs.

Required environment variables (add these as GitHub Actions *Secrets*):
  ANTHROPIC_API_KEY   Anthropic API key
  SMTP_HOST           e.g. smtp.gmail.com
  SMTP_USER           SMTP login (often the same as EMAIL_FROM)
  SMTP_PASS           SMTP password / app password
  EMAIL_FROM          From: address
  EMAIL_TO            where alerts + summaries go   <-- your address, kept hidden
Optional:
  SMTP_PORT           default 465 (SSL). Use 587 for STARTTLS.
  ANTHROPIC_MODEL     model id (set as an Actions *Variable*); verify the current
                      id at https://docs.claude.com/en/docs/about-claude/models

Manual test (no email, no state changes):
  python watcher.py --test https://nav.al/<episode-slug>
"""

import os
import sys
import json
import ssl
import html as html_lib
import hashlib
import smtplib
import datetime
import pathlib
import traceback
from email.message import EmailMessage

import requests
import feedparser
import trafilatura
from bs4 import BeautifulSoup
import anthropic

# ----------------------------- configuration --------------------------------

FEED_URL = "https://nav.al/feed/"

STATE_DIR = pathlib.Path("state")
LAST_SEEN_FILE = STATE_DIR / "last_seen.txt"
ALERT_FILE = STATE_DIR / "last_alert.json"
HEARTBEAT_FILE = STATE_DIR / "heartbeat.txt"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
MAX_TRANSCRIPT_CHARS = 500_000   # hard cap on what we send to the model
MIN_TRANSCRIPT_CHARS = 2_000     # below this we assume the transcript isn't up yet
ALERT_THROTTLE_HOURS = 6         # don't repeat the same alert more often than this
HTTP_TIMEOUT = 30
USER_AGENT = "naval-podcast-watcher/1.0 (+https://github.com)"

# --------------------------------- helpers ----------------------------------

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def log(msg):
    print(f"[{now_utc().isoformat(timespec='seconds')}] {msg}", flush=True)

def require_env(keys):
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        # Can't reliably email if the email config itself is missing, so just fail loudly;
        # the failed Actions run is the signal in that case.
        log(f"FATAL: missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

# ------------------------------- email --------------------------------------

def send_email(subject, body, html=None):
    """Send an email. If `html` is given, the message is multipart: clients that
    render HTML show the bold version, others fall back to the plain `body`."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]

    if port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=HTTP_TIMEOUT) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)
    log(f"Email sent: {subject!r}")

# ------------------------- alert throttling / state -------------------------

def _read_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def maybe_alert(subject, body, signature):
    """Send an alert email, but at most once per ALERT_THROTTLE_HOURS for the
    same signature (so a persistent outage doesn't spam you every 30 minutes)."""
    prev = _read_json(ALERT_FILE)
    if prev and prev.get("signature") == signature:
        try:
            last = datetime.datetime.fromisoformat(prev["ts"])
            if now_utc() - last < datetime.timedelta(hours=ALERT_THROTTLE_HOURS):
                log(f"Alert suppressed (throttled): {signature}")
                return
        except Exception:
            pass
    send_email(subject, body)
    STATE_DIR.mkdir(exist_ok=True)
    ALERT_FILE.write_text(json.dumps({"signature": signature, "ts": now_utc().isoformat()}))

def clear_alert():
    if ALERT_FILE.exists():
        ALERT_FILE.unlink()
        log("Cleared alert state (back to normal).")

def load_last_seen():
    try:
        return LAST_SEEN_FILE.read_text().strip() or None
    except Exception:
        return None

def save_last_seen(guid):
    STATE_DIR.mkdir(exist_ok=True)
    LAST_SEEN_FILE.write_text(guid.strip() + "\n")

def write_heartbeat():
    STATE_DIR.mkdir(exist_ok=True)
    # Only the date, so this file changes at most once per day -> at most one
    # heartbeat commit per day, which keeps the repo (and the schedule) alive.
    HEARTBEAT_FILE.write_text(now_utc().strftime("%Y-%m-%d") + "\n")

# ------------------------------ feed parsing --------------------------------

def is_podcast(entry):
    """nav.al/feed/ is the site-wide feed (blog posts AND podcasts). Treat an
    item as a podcast if it has an audio enclosure OR a category/tag mentioning
    'podcast'. If you find your feed marks episodes differently, tweak this."""
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).lower().startswith("audio"):
            return True
    for tag in entry.get("tags", []) or []:
        term = (tag.get("term") or "").lower()
        if "podcast" in term:
            return True
    return False

def entry_guid(entry):
    return entry.get("id") or entry.get("link")

def entry_date(entry):
    if entry.get("published_parsed"):
        dt = datetime.datetime(*entry.published_parsed[:6])
        return dt.strftime("%B %-d, %Y")   # e.g. "July 3, 2026"
    return entry.get("published", "")

# ----------------------------- transcript -----------------------------------

def _strip_html(html):
    return BeautifulSoup(html, "html.parser").get_text("\n").strip()

def get_transcript(entry):
    """Prefer full text embedded in the feed; otherwise scrape the episode page
    (where nav.al puts the transcript). Return the longest text found."""
    candidates = []
    for c in entry.get("content", []) or []:
        val = c.get("value")
        if val:
            candidates.append(_strip_html(val))
    link = entry.get("link")
    if link:
        try:
            r = requests.get(link, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            if r.ok:
                extracted = trafilatura.extract(r.text, include_comments=False, favor_recall=True)
                candidates.append((extracted or _strip_html(r.text)).strip())
        except Exception as e:
            log(f"Transcript page fetch failed for {link}: {e}")
    text = max(candidates, key=len) if candidates else ""
    return text[:MAX_TRANSCRIPT_CHARS]

# ----------------------------- summarisation --------------------------------

SYSTEM_PROMPT = (
    "You summarise podcast episodes into a fixed format. "
    "Output ONLY the summary, with no preamble, no sign-off, and no code fences."
)

FORMAT_INSTRUCTIONS = """You are given the full transcript of a Naval podcast episode. Write a summary in EXACTLY the format, tone, and style shown in the example below.

Rules:
- Output PLAIN TEXT only. Do NOT use asterisks, markdown, HTML, or any formatting characters. The email system adds the bold styling based on the structure below, so formatting depends on you following it precisely.
- First line, exactly this pattern:
  Naval Podcast · Naval Ravikant with <GUESTS> · <DATE>
  Replace <GUESTS> with the guest names you identify from the transcript, comma-separated (e.g. "Garry Tan, Daniel Francis, Farbood Nivi"). If there are no guests, write just "Naval Ravikant" with no "with" clause. Replace <DATE> with the publish date exactly as provided to you.
- Then a blank line.
- Then a series of themed sections. Each section is:
    <emoji> <TITLE IN ALL CAPS>
    <blank line>
    <one paragraph of plain, simple prose>
    <blank line>
  Pick an emoji that fits each theme. Each title line MUST be in ALL CAPS (this is how the email detects titles and makes them bold). Do not put the title in all caps if it isn't a title, and never write a body paragraph in all caps.
- Tone: calm, plain English, explanatory, concrete. Short words, no jargon. Attribute claims to the specific person who made them ("Garry Tan believes...", "Daniel Francis said..."). Match the example's voice closely.
- Length follows the substance of the episode. Only include themes that are genuinely relevant and important. Do NOT pad or invent sections to reach a length. A short or light episode should produce fewer and shorter sections; a long, dense episode more. Each paragraph should be about the length of those in the example (roughly 3-6 sentences), and shorter when the point is simple.
- Do not add any headline, intro, outro, links, or commentary outside this structure. Output only the summary.

Here is an example of the exact format, tone, and length to match:

Naval Podcast · Naval Ravikant with Garry Tan, Daniel Francis, Farbood Nivi · July 3, 2026

🚀 AI COMPUTE IS ABOUT TO EXPLODE

Garry Tan believes AI computing power will grow about 90,000 times bigger over the next two to three years. Prices for using AI are dropping fast, and companies are building huge data centers to support it. His point: even if he is off by a lot, chips like Nvidia's may still be worth more than people think, not less. Each big jump in computing power tends to unlock things AI simply could not do before.

🤖 CAN AI TRULY CREATE, OR JUST COMBINE?

Everyone agrees AI is already very smart. The real question is whether it can be truly original, not just remix things it has already learned. One moment stood out: AI recently made real progress on a famous unsolved math problem. A former math major on the call called it "creepy" — not because it was wrong, but because it looked genuinely new, not copied.

💰 THE REAL BOTTLENECK IS COST, NOT SMARTS

Daniel Francis built an AI system for his health company. He said the model being smart was never the hard part. The hard part was the price. His team started by spending $100 per person every month on AI. After three or four months of work, they got that down to $2.84 per person. His takeaway: AI is already smart enough for most jobs. The companies that win will be the ones who make it cheap, not the ones who make it smarter.

🇨🇳 WHY CHINA IS CATCHING UP SO FAST

The group explained why Chinese AI models are closing the gap quickly. China lets its AI learn from the entire internet without copyright rules holding it back. China also produces more top math and science graduates than anywhere else. American AI companies have weak security, so their work keeps getting leaked or copied. But the biggest reason may be this: computer chips are now mostly made in China, and AI can now write most computer code by itself, so code is no longer a big advantage either. That leaves only the deep science of building AI itself as the one thing still hard to copy. China's government funds many AI labs and asks them to share their work with each other, so they all improve faster as a group.

⚖️ THE RISK OF FEWER PEOPLE CONTROLLING AI

Sam Altman recently said new AI models will first go only to certain partners approved by the US government. The group sees this as an early sign that AI could become tightly controlled by a small group of people or one government. Their worry: it may actually be more dangerous for AI to be controlled by a few powerful people than for lots of people to have access to it, even though wide access brings its own risks too.

✍️ SHOULD YOU LET AI WRITE FOR YOU?

The group disagreed here. One person argued that if AI wrote something, it should go straight to your AI to read it for you — there is no point wasting a human's time reading machine-written text. Another pushed back, saying that once you build a strong enough personal AI trained on your own thinking and writing, it will soon be impossible to tell the difference between AI writing and your own. Refusing to use it, he argued, will put you at a real disadvantage.

😰 THE PEOPLE BUILDING AI ARE WORRIED TOO

The most striking moment of the conversation: many researchers working inside the top AI labs are reportedly anxious about the future. Some have said they are putting off having children because they are unsure what kind of world their kids will grow up in. The founders' response was that big technology shifts have always been disruptive. The shift away from farm work took about 60 years and was painful for many people along the way. This shift with AI is happening much faster, which is exactly why it feels more unsettling."""

def build_prompt(title, date_str, transcript):
    return (
        f"{FORMAT_INSTRUCTIONS}\n\n"
        "---\n"
        f"Episode title: {title}\n"
        f"Publish date: {date_str}\n"
        "Host: Naval Ravikant\n\n"
        "Transcript:\n"
        f"{transcript}\n"
    )

def summarize(title, date_str, transcript):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(title, date_str, transcript)}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

def summary_to_html(summary, link):
    """Turn the model's plain summary into HTML. The byline (first line) becomes a
    light subheading; each ALL-CAPS title line becomes real bold; everything else
    becomes a paragraph. Titles are detected by being ALL CAPS, which is why the
    model is told to keep them uppercase."""
    blocks = []
    byline_done = False
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        safe = html_lib.escape(line)
        if not byline_done:
            blocks.append(f'<p style="color:#666;font-style:italic;margin:0 0 18px 0">{safe}</p>')
            byline_done = True
            continue
        letters = [c for c in line if c.isalpha()]
        if letters and all(c.isupper() for c in letters):
            blocks.append(f'<p style="margin:22px 0 6px 0;font-size:16px"><b>{safe}</b></p>')
        else:
            blocks.append(f'<p style="margin:0 0 12px 0;line-height:1.55">{safe}</p>')
    if link:
        safe_link = html_lib.escape(link)
        blocks.append(f'<p style="margin:22px 0 0 0">🔗 <a href="{safe_link}">{safe_link}</a></p>')
    inner = "\n".join(blocks)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        'Helvetica,Arial,sans-serif;font-size:15px;color:#111;max-width:640px">'
        f"{inner}</div>"
    )

# ------------------------------- main flow ----------------------------------

def process_new_episode(entry):
    """Return True if a summary was sent, False if we deferred (transcript not up yet)."""
    guid = entry_guid(entry)
    title = entry.get("title", "(untitled)")
    date_str = entry_date(entry)
    link = entry.get("link", "")
    log(f"New podcast: {title!r} ({guid})")

    transcript = get_transcript(entry)
    if len(transcript) < MIN_TRANSCRIPT_CHARS:
        log(f"Transcript looks unavailable (len={len(transcript)}). Deferring to a later run.")
        maybe_alert(
            subject=f"🎙️ New Naval podcast (transcript not ready): {title}",
            body=(
                "A new episode is live, but the full transcript isn't on the page yet, so no "
                "summary was generated. The watcher will keep checking and send the summary "
                f"automatically once the transcript appears.\n\n{title}\n{link}"
            ),
            signature=f"pending:{guid}",
        )
        return False

    summary = summarize(title, date_str, transcript)
    send_email(
        subject=f"🎙️ New Naval Podcast: {title}",
        body=f"{summary}\n\n🔗 {link}",          # plain-text fallback
        html=summary_to_html(summary, link),      # bold titles for HTML clients
    )
    return True

def run():
    write_heartbeat()  # first, so even a failing run keeps the repo active (once/day)

    resp = requests.get(FEED_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(f"Feed returned HTTP {resp.status_code}")
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Feed did not parse: {getattr(feed, 'bozo_exception', 'unknown error')}")
    if not feed.entries:
        raise RuntimeError("Feed parsed but contained no items")

    podcasts = [e for e in feed.entries if is_podcast(e)]
    log(f"Feed OK: {len(feed.entries)} items, {len(podcasts)} podcast items.")

    if not podcasts:
        # Feed is fine but nothing matched the podcast filter. Not necessarily broken,
        # but flag it (throttled) in case Naval changed how episodes are tagged.
        maybe_alert(
            subject="⚠️ Naval watcher: no podcast items detected",
            body=("The feed loaded fine but no items matched the podcast filter. "
                  "If Naval changed how episodes are tagged/enclosed, update is_podcast() "
                  "in watcher.py."),
            signature="no-podcasts",
        )
        return

    last_seen = load_last_seen()

    # First ever run: set a baseline so you don't get emailed about existing episodes.
    if last_seen is None:
        newest = entry_guid(podcasts[0])
        save_last_seen(newest)
        log(f"First run — baseline set to {newest}. No email sent.")
        clear_alert()
        return

    # Podcast items newer than last_seen (feed is newest-first).
    new_items = []
    for e in podcasts:
        if entry_guid(e) == last_seen:
            break
        new_items.append(e)
    else:
        # last_seen fell off the current feed window; be conservative, take only the newest.
        new_items = podcasts[:1]
        log("last_seen guid not found in feed; treating only the newest item as new.")

    if not new_items:
        log("No new episodes.")
        clear_alert()
        return

    # Oldest first, so state only advances past episodes we actually sent.
    for e in reversed(new_items):
        if not process_new_episode(e):
            # Transcript not ready: stop so we retry this one next run instead of skipping it.
            break
        save_last_seen(entry_guid(e))

    clear_alert()

def main():
    require_env(["ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM", "EMAIL_TO"])
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        log("ERROR:\n" + tb)
        last_line = tb.strip().splitlines()[-1] if tb.strip() else "Unknown error"
        sig = "error:" + hashlib.sha1(last_line.encode()).hexdigest()[:12]
        try:
            maybe_alert(
                subject="⚠️ Naval Podcast Watcher — something broke",
                body=("The watcher hit an error while checking nav.al. It will retry on the next "
                      "run (every 30 min). Details:\n\n" + tb),
                signature=sig,
            )
        except Exception:
            log("Also failed to send the alert email.")
        sys.exit(1)

def test_mode(url):
    """Summarise a single episode URL and print it. No email, no state changes."""
    require_env(["ANTHROPIC_API_KEY"])
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    text = (trafilatura.extract(r.text, include_comments=False, favor_recall=True) or _strip_html(r.text))
    text = text[:MAX_TRANSCRIPT_CHARS]
    log(f"Transcript characters extracted: {len(text)}")
    print("\n" + summarize("(test)", now_utc().strftime("%B %-d, %Y"), text) + "\n")

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        test_mode(sys.argv[2])
    else:
        main()
