#!/usr/bin/env python3
"""
Collect all Mon-Friday messages from a source Slack channel for *last week*
and post a single combined digest to a destination channel.

Usage:
    SLACK_TOKEN=xoxb-... python3 slack_weekly_digest.py \
        --source C0ASUA49PDY --dest C0BDW158FKP

Options:
    --tz            IANA timezone for the Mon-Fri window (default: local)
    --week-offset   0 = current week, 1 = last week (default: 1)
    --dry-run       Print the digest instead of posting it
"""
import argparse, datetime, json, os, sys, time, urllib.request, urllib.error
from zoneinfo import ZoneInfo

API = "https://slack.com/api/"


def call(method, token, params=None, post=False):
    """Call a Slack Web API method, returning the parsed JSON."""
    url = API + method
    headers = {"Authorization": f"Bearer {token}"}
    if post:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                wait = int(e.headers.get("Retry-After", "2"))
                time.sleep(wait)
                continue
            raise
        if not body.get("ok") and body.get("error") == "ratelimited":
            time.sleep(2)
            continue
        return body
    return body


_user_cache = {}


def user_name(uid, token):
    if not uid:
        return "unknown"
    if uid in _user_cache:
        return _user_cache[uid]
    info = call("users.info", token, {"user": uid})
    if info.get("ok"):
        u = info["user"]
        name = u.get("profile", {}).get("real_name") or u.get("name") or uid
    else:
        name = uid
    _user_cache[uid] = name
    return name


def upload_file_snippet(token, channel, content, filename, title, initial_comment):
    """Upload `content` as a single file snippet using Slack's external upload flow."""
    data = content.encode("utf-8")
    # 1) get an upload URL
    res = call("files.getUploadURLExternal", token,
               {"filename": filename, "length": str(len(data))})
    if not res.get("ok"):
        sys.exit(f"getUploadURLExternal failed: {res.get('error')}")
    upload_url, file_id = res["upload_url"], res["file_id"]
    # 2) POST the bytes to the upload URL
    req = urllib.request.Request(upload_url, data=data,
                                 headers={"Content-Type": "text/plain"}, method="POST")
    with urllib.request.urlopen(req) as r:
        r.read()
    # 3) complete the upload, sharing into the channel with a comment
    payload = {"files": [{"id": file_id, "title": title}], "channel_id": channel}
    if initial_comment:
        payload["initial_comment"] = initial_comment
    res = call("files.completeUploadExternal", token, payload, post=True)
    if not res.get("ok"):
        sys.exit(f"completeUploadExternal failed: {res.get('error')}")
    return res


def prune_old_digests(token, channel, keep):
    """Keep only the `keep` most recent weekly-digest files in the channel,
    deleting older ones so the channel doesn't get messy."""
    cursor, items = None, []
    while True:
        params = {"channel": channel, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = call("conversations.history", token, params)
        if not r.get("ok"):
            break
        for m in r.get("messages", []):
            files = m.get("files") or []
            if any((f.get("name", "").startswith("weekly-digest-")) for f in files):
                items.append(m["ts"])
        cursor = r.get("response_metadata", {}).get("next_cursor")
        if not r.get("has_more") or not cursor:
            break
    items.sort(key=float, reverse=True)  # newest first
    removed = 0
    for ts in items[keep:]:
        if call("chat.delete", token, {"channel": channel, "ts": ts}, post=True).get("ok"):
            removed += 1
        time.sleep(0.3)
    return len(items), removed


def fetch_history(channel, token, oldest, latest):
    """Fetch all top-level messages in [oldest, latest)."""
    msgs, cursor = [], None
    while True:
        params = {
            "channel": channel,
            "oldest": f"{oldest:.6f}",
            "latest": f"{latest:.6f}",
            "inclusive": "false",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        res = call("conversations.history", token, params)
        if not res.get("ok"):
            sys.exit(f"conversations.history failed: {res.get('error')}")
        msgs.extend(res.get("messages", []))
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not res.get("has_more") or not cursor:
            break
    msgs.sort(key=lambda m: float(m["ts"]))
    return msgs


def fetch_replies(channel, token, thread_ts):
    res = call("conversations.replies", token,
               {"channel": channel, "ts": thread_ts, "limit": 200})
    if not res.get("ok"):
        return []
    # first element is the parent itself
    return res.get("messages", [])[1:]


def fmt(m, token, tz):
    ts = float(m["ts"])
    when = datetime.datetime.fromtimestamp(ts, tz).strftime("%a %m/%d %H:%M")
    who = user_name(m.get("user") or m.get("bot_id"), token)
    text = m.get("text", "").strip() or "(no text)"
    return when, who, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--tz", default=None)
    ap.add_argument("--week-offset", type=int, default=1)
    ap.add_argument("--keep", type=int, default=2,
                    help="how many recent weekly files to keep in the channel (0 = no pruning)")
    ap.add_argument("--include-threads", action="store_true", default=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("SLACK_TOKEN")
    if not token:
        sys.exit("Set SLACK_TOKEN env var")

    tz = ZoneInfo(args.tz) if args.tz else datetime.datetime.now().astimezone().tzinfo

    today = datetime.datetime.now(tz).date()
    this_mon = today - datetime.timedelta(days=today.weekday())
    mon = this_mon - datetime.timedelta(days=7 * args.week_offset)
    sat = mon + datetime.timedelta(days=5)  # exclusive end = Sat 00:00 (covers Mon-Fri)

    oldest = datetime.datetime.combine(mon, datetime.time(0, 0), tz).timestamp()
    latest = datetime.datetime.combine(sat, datetime.time(0, 0), tz).timestamp()
    fri = mon + datetime.timedelta(days=4)

    print(f"Window: {mon} (Mon) 00:00 -> {fri} (Fri) 23:59  [{tz}]", file=sys.stderr)

    msgs = fetch_history(args.source, token, oldest, latest)
    # keep real human/bot messages, drop channel-join/leave noise
    msgs = [m for m in msgs if m.get("type") == "message"
            and m.get("subtype") not in ("channel_join", "channel_leave")]

    if args.include_threads:
        expanded = []
        for m in msgs:
            expanded.append(m)
            if int(m.get("reply_count", 0)) > 0:
                for r in fetch_replies(args.source, token, m["ts"]):
                    if oldest <= float(r["ts"]) < latest:
                        r["_reply"] = True
                        expanded.append(r)
        expanded.sort(key=lambda m: float(m["ts"]))
        msgs = expanded

    if not msgs:
        digest = f"*Weekly digest — {mon:%b %d} to {fri:%b %d}, {mon:%Y}*\n_No messages found in <#{args.source}> for this period._"
    else:
        lines = [f"*Weekly digest — {mon:%b %d} to {fri:%b %d}, {mon:%Y}*",
                 f"_{len(msgs)} messages from <#{args.source}>_", ""]
        cur_day = None
        for m in msgs:
            when, who, text = fmt(m, token, tz)
            day = when.split()[0:2]  # e.g. ['Mon', '06/22']
            day_key = " ".join(day)
            if day_key != cur_day:
                lines.append(f"\n*— {day_key} —*")
                cur_day = day_key
            t = when.split()[-1]
            prefix = "    ↳ " if m.get("_reply") else "• "
            lines.append(f"{prefix}`{t}` *{who}*: {text}")
        digest = "\n".join(lines)

    # Send as ONE combined message. Slack hard limit is 40k chars for text;
    # if the digest is larger we upload it as a single file snippet instead
    # (still one item in the channel) rather than splitting into many messages.
    if args.dry_run:
        print(digest)
        print(f"\n[dry-run] digest length = {len(digest)} chars", file=sys.stderr)
        return

    # Slack splits any single chat message >~4000 chars into multiple messages.
    # To guarantee ONE channel item, only use a plain message when it's short
    # enough; otherwise upload the whole digest as a single file snippet.
    if len(digest) <= 3900:
        res = call("chat.postMessage", token,
                   {"channel": args.dest, "text": digest, "unfurl_links": False},
                   post=True)
        if not res.get("ok"):
            sys.exit(f"chat.postMessage failed: {res.get('error')}")
        print(f"Sent single message ts={res['ts']} ({len(digest)} chars)", file=sys.stderr)
    else:
        # Upload as a single text snippet (one channel item) using Slack's
        # current 3-step external upload flow (files.upload is deprecated).
        upload_file_snippet(
            token, args.dest, digest,
            filename=f"weekly-digest-{mon:%Y%m%d}.txt",
            title=f"Weekly digest {mon:%b %d}-{fri:%b %d} {mon:%Y}",
            initial_comment="")  # file only, no comment
        print(f"Uploaded digest as single snippet ({len(digest)} chars)", file=sys.stderr)

    # Retention: keep only the N most recent weekly files in the channel.
    if args.keep and args.keep > 0:
        time.sleep(3)  # let the just-posted file propagate into channel history
        total, removed = prune_old_digests(token, args.dest, args.keep)
        print(f"Retention: {total} digest file(s) present, removed {removed} old one(s), "
              f"keeping newest {args.keep}", file=sys.stderr)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    import urllib.parse
    main()
