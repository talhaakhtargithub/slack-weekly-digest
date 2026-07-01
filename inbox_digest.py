'''
Inbox Digest — one daily log of "which emails came" across every campaign account
=================================================================================
Instead of logging into 7-8 Gmail inboxes by hand, this connects to all of them
over IMAP and prints ONE combined, categorised list of incoming mail.

Usage:
    python inbox_digest.py                 # last 1 day (today's log)
    python inbox_digest.py --days 3        # last 3 days
    python inbox_digest.py --replies-only  # hide bounces + automated noise
    python inbox_digest.py --save          # also write logs/inbox_YYYY-MM-DD.txt

Credentials: read from the GMAIL_ACCOUNTS json block in .env (same as the campaign).
Accounts whose app-password isn't in .env are listed as "skipped" — paste their
app-password into .env to include them.
Never sends anything; read-only IMAP.
'''
import os
import re
import sys
import glob
import json
import time
import argparse
import imaplib
import email
import urllib.request
import urllib.error
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent


def load_accounts():
    """email -> app_password.

    In GitHub Actions, set secret DIGEST_ACCOUNTS_JSON to a JSON list of
    {"email":..,"password":..} and it's used directly. Locally, falls back to the
    GMAIL_ACCOUNTS json block in .env (and any campaign-slot .env).
    """
    import json
    blob = os.environ.get("DIGEST_ACCOUNTS_JSON", "").strip()
    if blob:
        try:
            return {a["email"].strip(): (a.get("password") or "").replace(" ", "")
                    for a in json.loads(blob) if a.get("email") and a.get("password")}
        except Exception as e:
            print(f"# DIGEST_ACCOUNTS_JSON parse failed ({e}); falling back to .env")
    creds = {}
    sources = [ROOT / ".env"] + [Path(p) for p in glob.glob(str(Path.home() / "campaign-slots/*/.env"))]
    for s in sources:
        try:
            txt = Path(s).read_text()
        except Exception:
            continue
        for e, p in re.findall(r'"email":\s*"([^"]+)"[^}]*?"password":\s*"([^"]+)"', txt):
            creds.setdefault(e.strip(), p.replace(" ", ""))
    return creds


def _dec(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def classify(frm, subj):
    f, s = (frm or "").lower(), (subj or "").lower()
    if ("mailer-daemon" in f or "postmaster" in f
            or "undeliverable" in s or "delivery status notification" in s
            or "mail delivery failed" in s or "returning message to sender" in s):
        return "BOUNCE"
    if ("no-reply" in f or "noreply" in f or "notifications@" in f or "notification" in f
            or "google.com" in f or "github.com" in f or "accounts.google" in f):
        return "AUTO"
    return "REPLY"          # real human / application response


def _body_text(msg, limit=1500):
    """Plain-text body of a message, truncated."""
    parts = []
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition", "")):
                try:
                    parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"))
                except Exception:
                    pass
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:
            pass
    text = "\n".join(parts)
    text = re.sub(r'^>.*$', '', text, flags=re.M)          # drop quoted lines
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text[:limit]


def fetch_account(acct, pw, since_dt, want_bodies=False):
    imap_since = since_dt.strftime("%d-%b-%Y")
    out = []
    # connect + login with retries (IMAP/Gmail occasionally drops connections)
    M = None
    last = None
    for attempt in range(3):
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
            M.login(acct, pw)
            break
        except Exception as e:
            last = e
            try:
                M.logout()
            except Exception:
                pass
            M = None
            time.sleep(min(2 ** attempt, 8))
    if M is None:
        raise last if last else RuntimeError("imap connect failed")
    M.select("INBOX")
    typ, data = M.search(None, f'(SINCE {imap_since})')
    for i in data[0].split():
        typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        frm = parseaddr(_dec(msg.get("From")))[1]
        subj = _dec(msg.get("Subject"))
        try:
            dt = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc)
        except Exception:
            dt = since_dt
        if dt < since_dt:
            continue
        kind = classify(frm, subj)
        body = ""
        if want_bodies and kind == "REPLY":
            try:
                typ, fd = M.fetch(i, "(BODY.PEEK[])")
                if fd and fd[0]:
                    body = _body_text(email.message_from_bytes(fd[0][1]))
            except Exception:
                pass
        out.append([dt, acct, frm, subj, kind, body])
    M.logout()
    return out


INTENTS = ("interested", "meeting-request", "needs-info", "rejected",
           "auto-reply", "ticket-ack", "newsletter", "other")


def _nvidia_chat(key, model, prompt, tries=3, timeout=45):
    """One NVIDIA chat call with retries + exponential backoff. Raises on final failure."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": 80,
    }).encode()
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions", data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            # 4xx (bad request/auth) won't fix on retry — stop early, except 429 rate-limit
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last = str(e)[:50]
        time.sleep(min(2 ** attempt, 8))          # 1s, 2s, 4s, capped 8s
    raise RuntimeError(last or "unknown")


def llm_enrich(frm, subj, body):
    """Return (intent, one_line_summary) via NVIDIA LLM, with retries + a fallback model.
    Never raises — returns ('other', note) if every attempt fails."""
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        return "", ""
    primary = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
    fallback = os.environ.get("NVIDIA_MODEL_FALLBACK", "meta/llama-3.1-70b-instruct")
    models = [primary] + ([fallback] if fallback and fallback != primary else [])
    prompt = (
        "You classify replies to my job/service outreach emails.\n"
        f"Intent must be exactly one of: {', '.join(INTENTS)}.\n"
        "Return ONLY compact JSON: {\"intent\":\"<one>\",\"summary\":\"<max 12 words>\"}.\n\n"
        f"From: {frm}\nSubject: {subj}\nBody:\n{body[:1200]}"
    )
    last_err = ""
    for model in models:
        try:
            txt = _nvidia_chat(key, model, prompt)
            m = re.search(r'\{.*\}', txt, re.S)
            obj = json.loads(m.group(0)) if m else {}
            intent = str(obj.get("intent", "other")).strip().lower()
            if intent not in INTENTS:
                intent = "other"
            return intent, str(obj.get("summary", "")).strip()[:80]
        except Exception as e:
            last_err = str(e)[:40]
            continue                               # try the fallback model
    return "other", f"(llm unavailable: {last_err})"


def _chunk_lines(report, limit=3600):
    chunks, cur = [], ""
    for line in report.splitlines():
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


def send_slack_bot(report):
    """Push the digest to Slack via a bot token (SLACK_BOT_TOKEN + SLACK_CHANNEL).

    Posts in <=3600-char code-block chunks via chat.postMessage. Returns status.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = os.environ.get("SLACK_CHANNEL", "").strip()
    if not token or not channel:
        return "slack-bot: skipped (no token/channel)"
    chunks = _chunk_lines(report)
    sent = 0
    for idx, ch in enumerate(chunks):
        prefix = "📬 *Inbox digest*\n" if idx == 0 else f"_(cont. {idx+1}/{len(chunks)})_\n"
        payload = json.dumps({"channel": channel, "text": prefix + "```\n" + ch + "```",
                              "mrkdwn": True}).encode()
        err = None
        for attempt in range(3):                   # retry transient failures
            req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=payload,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"})
            try:
                resp = json.load(urllib.request.urlopen(req, timeout=30))
                if resp.get("ok"):
                    err = None
                    break
                err = resp.get("error")
                # config errors (bad channel/auth) won't fix on retry
                if err in ("channel_not_found", "not_in_channel", "invalid_auth", "not_authed"):
                    return f"slack-bot: error '{err}' after {sent} ok"
            except Exception as e:
                err = str(e)[:50]
            time.sleep(min(2 ** attempt, 8))
        if err:
            return f"slack-bot: chunk {idx+1} failed after {sent} ok ({err})"
        sent += 1
    return f"slack-bot: sent ({sent} message(s))"


def send_slack(report):
    """Push the digest to Slack via an Incoming Webhook (SLACK_WEBHOOK_URL)."""
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return "slack: skipped (no webhook url)"
    chunks = _chunk_lines(report)
    sent = 0
    for idx, ch in enumerate(chunks):
        prefix = "📬 *Inbox digest*\n" if idx == 0 else f"_(cont. {idx+1}/{len(chunks)})_\n"
        payload = json.dumps({"text": prefix + "```\n" + ch + "```"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}), timeout=30).read()
            sent += 1
        except Exception as e:
            return f"slack: chunk {idx+1} failed after {sent} ok ({str(e)[:50]})"
    return f"slack: sent ({sent} message(s))"


def send_email(report):
    """Email the digest to DIGEST_EMAIL_TO, sent from the first digest account's SMTP."""
    to = os.environ.get("DIGEST_EMAIL_TO", "").strip()
    if not to:
        return "email: skipped (no recipient)"
    creds = load_accounts()
    if not creds:
        return "email: skipped (no sender account)"
    sender, pw = next(iter(creds.items()))
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(report, _charset="utf-8")
    msg["Subject"] = f"📬 Inbox digest — {datetime.now(timezone.utc):%Y-%m-%d}"
    msg["From"] = sender
    msg["To"] = to
    last = ""
    for attempt in range(3):                       # retry transient SMTP failures
        try:
            S = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            S.login(sender, pw)
            S.sendmail(sender, [to], msg.as_string())
            S.quit()
            return f"email: sent to {to} (from {sender})"
        except smtplib.SMTPAuthenticationError as e:
            return f"email: auth failed ({str(e)[:50]})"   # won't fix on retry
        except Exception as e:
            last = str(e)[:60]
            time.sleep(min(2 ** attempt, 8))
    return f"email: failed after retries ({last})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--replies-only", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--llm-limit", type=int, default=40,
                    help="max replies to summarize with the LLM per run")
    ap.add_argument("--slack-bot", action="store_true",
                    help="post the digest to Slack via bot token (SLACK_BOT_TOKEN + SLACK_CHANNEL)")
    ap.add_argument("--email", action="store_true",
                    help="email the digest to DIGEST_EMAIL_TO")
    args = ap.parse_args()

    creds = load_accounts()
    llm_on = bool(os.environ.get("NVIDIA_API_KEY", "").strip())
    since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    rows, skipped, failed = [], [], []
    for acct, pw in creds.items():
        try:
            rows.extend(fetch_account(acct, pw, since_dt, want_bodies=llm_on))
        except Exception as e:
            failed.append(f"{acct}: {str(e)[:60]}")

    rows.sort(reverse=True, key=lambda r: r[0])

    # LLM summary + intent for real replies (capped to keep runs short/cheap)
    enrich = {}
    if llm_on:
        reply_rows = [r for r in rows if r[4] == "REPLY"][:args.llm_limit]
        for r in reply_rows:
            dt, acct, frm, subj, kind, body = r
            enrich[id(r)] = llm_enrich(frm, subj, body)
    lines = []
    lines.append(f"# Inbox digest — last {args.days} day(s), generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    lines.append(f"# accounts read: {len(creds)}   messages: {len(rows)}")
    lines.append("")

    # summary per account
    by_acct = {}
    for dt, acct, frm, subj, kind, body in rows:
        d = by_acct.setdefault(acct, {"REPLY": 0, "BOUNCE": 0, "AUTO": 0})
        d[kind] += 1
    lines.append("## Per-account summary (replies / bounces / auto)")
    for acct in sorted(creds):
        d = by_acct.get(acct, {"REPLY": 0, "BOUNCE": 0, "AUTO": 0})
        lines.append(f"  {acct:34} replies={d['REPLY']:3}  bounces={d['BOUNCE']:3}  auto={d['AUTO']:3}")
    lines.append("")

    # intent breakdown (when LLM is on)
    if enrich:
        by_intent = {}
        for k, (intent, _) in enrich.items():
            by_intent[intent] = by_intent.get(intent, 0) + 1
        lines.append("## Reply intents (LLM): " + ", ".join(
            f"{k}={v}" for k, v in sorted(by_intent.items(), key=lambda x: -x[1])))
        lines.append("")

    show = [r for r in rows if not (args.replies_only and r[4] != "REPLY")]
    lines.append(f"## Messages ({'replies only' if args.replies_only else 'all'})")
    for r in show:
        dt, acct, frm, subj, kind, body = r
        base = f"{dt:%m-%d %H:%M} | {kind:6} | {acct.split('@')[0]:20} | {frm[:30]:30} | {subj[:46]}"
        if id(r) in enrich:
            intent, summ = enrich[id(r)]
            base += f"\n            ↳ [{intent}] {summ}"
        lines.append(base)

    if skipped or failed:
        lines.append("")
    if failed:
        lines.append("## Login/read failures: " + "; ".join(failed))

    report = "\n".join(lines)
    print(report)

    log_file = None
    if args.save:
        logdir = ROOT / "logs"
        logdir.mkdir(exist_ok=True)
        log_file = logdir / f"inbox_{datetime.now(timezone.utc):%Y-%m-%d}.txt"
        log_file.write_text(report + "\n")
        if args.save:
            print(f"\n[saved] {log_file}")

    if args.slack_bot:
        print(send_slack_bot(report))
    if args.email:
        print(send_email(report))


if __name__ == "__main__":
    main()
