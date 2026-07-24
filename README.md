# Naval Podcast Watcher

Checks [nav.al](https://nav.al/feed/) every 30 minutes. When a **new podcast
episode** appears, it scrapes the transcript, writes a summary in a fixed
format, and emails it to you. It also emails you if the **feed or the check
itself breaks**. No email is sent on a normal "nothing new" run.

## How it works

1. Fetches the site RSS feed and keeps only items that are podcasts (audio
   enclosure, or a `podcast` category).
2. Compares the newest podcast's `<guid>` against the last one it handled
   (stored in `state/last_seen.txt`). Same → do nothing. New → continue.
3. Pulls the transcript from the episode page, summarises it via the Anthropic
   API, and emails you the summary.
4. On any failure it emails an alert (throttled to once every 6 hours per
   distinct error, so an outage doesn't spam you).

State is committed back to the repo after each run, so it persists between runs.

## Setup

1. **Create a new PUBLIC GitHub repo** and add these three files:
   `watcher.py`, `requirements.txt`, `.github/workflows/naval-podcast.yml`.
   (Public = unlimited free Actions minutes; keys stay safe in Secrets.)

2. **Add Secrets** — repo **Settings → Secrets and variables → Actions → Secrets**:

   | Secret | Example / notes |
   |---|---|
   | `ANTHROPIC_API_KEY` | your Anthropic API key |
   | `EMAIL_TO` | where summaries + alerts go (your address — stays hidden) |
   | `EMAIL_FROM` | the From: address |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_USER` | usually same as `EMAIL_FROM` |
   | `SMTP_PASS` | SMTP password / **Gmail App Password** (not your login password) |
   | `SMTP_PORT` | optional — `465` (default, SSL) or `587` (STARTTLS) |

   For Gmail: enable 2-Step Verification, then create an **App Password**
   (Google Account → Security → App passwords) and use that as `SMTP_PASS`.

3. **Add a Variable** — same page, **Variables** tab:

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_MODEL` | a current model id from the [models docs](https://docs.claude.com/en/docs/about-claude/models) |

4. **Enable Actions** for the repo if prompted. The schedule starts on its own.
   You can also hit **Run workflow** on the Actions tab to trigger it manually.

## First run

The first run just records the current latest episode as the baseline and sends
**no email**. You'll only get summaries for episodes published *after* that.

## Testing the summary format before an episode drops

Locally (needs only `ANTHROPIC_API_KEY` set in your shell):

```bash
pip install -r requirements.txt
python watcher.py --test https://nav.al/future
```

It prints the summary to your terminal — no email, no state changes. Good for
checking the model id works and the format looks right.

## Two knobs you may need to tune after the first real episode

- **`is_podcast()`** — the site feed contains blog posts too. This filters to
  audio-enclosure / `podcast`-category items. If a real episode ever fails to
  trigger, check one item's fields and loosen this.
- **`MIN_TRANSCRIPT_CHARS`** (default 2000) — nav.al sometimes posts the
  transcript a little after the audio. If the extracted text is shorter than
  this, the watcher assumes the transcript isn't up yet, emails a short
  "transcript not ready" heads-up, and retries next run until it appears. Raise
  or lower this once you've seen what a real transcript page extracts to.

## Things to know

- **GitHub cron is best-effort.** "Every 30 min" can drift by several minutes,
  and rarely a run is skipped. Fine for this; not clockwork.
- **Emails are HTML with real bold titles.** The model outputs clean plain text
  (no asterisks); `summary_to_html()` styles the byline and bolds the ALL-CAPS
  title lines. A plain-text version is included as a fallback for clients that
  don't render HTML. (`python watcher.py --test` prints the raw plain text.)
- **60-day inactivity rule.** GitHub disables scheduled workflows on a repo with
  no activity for 60 days. The daily heartbeat commit is meant to prevent this,
  but bot commits don't always reset the timer — if you hit a long gap between
  episodes, either push something occasionally or add a keepalive action
  (e.g. `gautamkrishnar/keepalive-workflow`).
