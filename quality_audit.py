#!/usr/bin/env python3
"""
Quality audit — bugs fixed per week, broken out by priority.

A week runs Sunday through Saturday in the Jira account's own timezone, so
buckets line up with what the Jira UI shows.

New counts bugs created inside the week — a flow, total only.

Fixed counts each bug once, in the week of its most recent transition into a
done status, at the priority it held then — also a flow.

Open is not a flow but a snapshot: how many bugs stood un-closed at 23:59:59
on the week's Saturday, by the priority they held at that instant, regardless
of when they were opened. Both status and priority are replayed to that moment
from the changelog.

Priorities are matched by id, not name: this instance was renamed from the
default scheme (Highest/Medium/...) to P0-P4, so historical changelog entries
carry names that no longer exist. The ids are stable across that rename.

Usage:
    python3 quality_audit.py
    python3 quality_audit.py --weeks 8
    python3 quality_audit.py --since 2026-07-01
    python3 quality_audit.py --csv
    python3 quality_audit.py --metric open
"""

import argparse
import os
import re
import sys
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip3 install requests --break-system-packages")
    sys.exit(1)

ENV_FILE = Path(os.environ.get("JIRA_ENV_FILE") or Path(__file__).with_name(".env"))


def load_env_file(path):
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file(ENV_FILE)
BASE_URL = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "").strip()
PROJECT = os.environ.get("JIRA_PROJECT_KEY", "").strip()
BUG_TYPE_NAMES = ("bug",)
TIMEOUT = 30
_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


class Jira:
    def __init__(self, token):
        cred = b64encode(f"{EMAIL}:{token}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Basic {cred}",
                                     "Accept": "application/json"})

    def get(self, path, **params):
        r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
        if r.status_code in (401, 403):
            sys.exit(f"Jira returned {r.status_code} for {path}. Check JIRA_API_TOKEN.")
        r.raise_for_status()
        return r.json()


def parse_dt(s):
    text = _OFFSET_RE.sub(r"\1:\2", s.strip().replace("Z", "+00:00"))
    dt = datetime.fromisoformat(text)
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)


def changes_for_field(issue, field_id=None, field_name=None):
    out = []
    for history in issue.get("changelog", {}).get("histories", []):
        when = parse_dt(history["created"])
        for item in history.get("items", []):
            if (field_id and item.get("fieldId") == field_id) or \
               (field_name and item.get("field") == field_name):
                out.append((when, item))
    out.sort(key=lambda pair: pair[0])
    return out


def fetch_bugs(jira):
    """Every bug in the project, with full changelogs."""
    issues, start = [], 0
    while True:
        data = jira.get("/rest/api/3/search/jql",
                        jql=f'project = "{PROJECT}" AND type = Bug ORDER BY created ASC',
                        fields="summary,created,status,priority,issuetype",
                        expand="changelog", maxResults=100, startAt=start)
        page = data.get("issues", [])
        issues += page
        if not page or len(issues) >= data.get("total", len(issues)):
            break
        start += len(page)
    for i in issues:
        cl = i.get("changelog", {})
        if cl.get("total", 0) > len(cl.get("histories", [])):
            hist, s2 = [], 0
            while True:
                d = jira.get(f"/rest/api/3/issue/{i['key']}/changelog", startAt=s2, maxResults=100)
                hist += d.get("values", [])
                if d.get("isLast", True) or not d.get("values"):
                    break
                s2 += len(d["values"])
            i["changelog"] = {"histories": hist}
    return issues


def status_id_at(issue, when):
    """Status id held at a point in time, replayed from the changelog."""
    value, seen = None, False
    for changed_at, item in changes_for_field(issue, field_id="status"):
        if changed_at <= when:
            value, seen = str(item.get("to")), True
        else:
            if not seen:
                value, seen = str(item.get("from")), True
            break
    if not seen:
        value = str(issue["fields"].get("status", {}).get("id", ""))
    return value


def last_done_at(issue, done_ids):
    """Most recent transition into a done status; None if there never was one."""
    last = None
    for when, item in changes_for_field(issue, field_id="status"):
        if str(item.get("to")) in done_ids:
            last = when
    return last


def priority_id_at(issue, when):
    """Priority id held at a point in time, replayed from the changelog."""
    value, seen = None, False
    for changed_at, item in changes_for_field(issue, field_name="priority"):
        if changed_at <= when:
            value, seen = str(item.get("to")), True
        else:
            if not seen:
                value, seen = str(item.get("from")), True
            break
    if not seen:
        value = str((issue["fields"].get("priority") or {}).get("id", ""))
    return value


def short_priority(name):
    head = name.split(" - ")[0].strip()
    return head if re.fullmatch(r"P\d+", head) else name


def week_start(local_dt):
    """Sunday 00:00 of the week containing local_dt."""
    offset = (local_dt.weekday() + 1) % 7          # Mon=0..Sun=6 -> Sun=0
    midnight = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=offset)


def main():
    ap = argparse.ArgumentParser(description="Bugs fixed per week by priority")
    ap.add_argument("--weeks", type=int, help="Only the most recent N weeks")
    ap.add_argument("--since", help="Earliest week to report (YYYY-MM-DD)")
    ap.add_argument("--until", help="Latest week to report (YYYY-MM-DD)")
    ap.add_argument("--metric", choices=("new", "fixed", "open", "all"), default="all",
                    help="Which columns to show (default: all)")
    ap.add_argument("--csv", action="store_true", help="Comma-separated, for pasting into a sheet")
    args = ap.parse_args()

    missing = [n for n, v in (("JIRA_BASE_URL", BASE_URL), ("JIRA_EMAIL", EMAIL),
                              ("JIRA_PROJECT_KEY", PROJECT)) if not v]
    if missing:
        sys.exit(f"Missing required config: {', '.join(missing)}. Set them in {ENV_FILE}.")
    token = os.environ.get("JIRA_API_TOKEN")
    if not token:
        sys.exit(f"Missing JIRA_API_TOKEN. Set it in the environment or in {ENV_FILE}.")

    jira = Jira(token)
    tz = ZoneInfo(jira.get("/rest/api/3/myself")["timeZone"])
    done_ids = {str(s["id"]) for s in jira.get("/rest/api/3/status")
                if s.get("statusCategory", {}).get("key") == "done"}
    priorities = jira.get("/rest/api/3/priority")
    order = [str(p["id"]) for p in priorities]
    labels = {str(p["id"]): p["name"] for p in priorities}

    bugs = fetch_bugs(jira)
    blank = lambda: {pid: 0 for pid in order}
    bump = lambda d, pid: d.__setitem__(pid, d.get(pid, 0) + 1)

    new_counts, fixed, no_transition, unfixed = {}, {}, [], 0
    for bug in bugs:
        created = parse_dt(bug["fields"]["created"])
        wk = week_start(created.astimezone(tz))
        new_counts[wk] = new_counts.get(wk, 0) + 1

        fixed_at = last_done_at(bug, done_ids)
        if fixed_at is None:
            if bug["fields"].get("status", {}).get("statusCategory", {}).get("key") == "done":
                no_transition.append(bug["key"])
            else:
                unfixed += 1
            continue
        bump(fixed.setdefault(week_start(fixed_at.astimezone(tz)), blank()),
             priority_id_at(bug, fixed_at))

    present = set(new_counts) | set(fixed)
    if not present:
        sys.exit("No bugs found.")

    now = datetime.now(tz)
    first, last = min(present), max(max(present), week_start(now))
    if args.since:
        first = max(first, week_start(datetime.fromisoformat(args.since).replace(tzinfo=tz)))
    if args.until:
        last = min(last, week_start(datetime.fromisoformat(args.until).replace(tzinfo=tz)))
    weeks, cursor = [], first
    while cursor <= last:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    if args.weeks:
        weeks = weeks[-args.weeks:]

    # Open backlog as of 23:59:59.999999 on each week's Saturday, capped at now
    # so the current partial week reports the present rather than the future.
    partial = False
    open_counts = {}
    for wk in weeks:
        as_of = wk + timedelta(days=7) - timedelta(microseconds=1)
        if as_of > now:
            as_of, partial = now, True
        counts = blank()
        for bug in bugs:
            if parse_dt(bug["fields"]["created"]) > as_of:
                continue
            if status_id_at(bug, as_of) in done_ids:
                continue
            bump(counts, priority_id_at(bug, as_of))
        open_counts[wk] = counts

    stray = {pid for c in list(fixed.values()) + list(open_counts.values())
             for pid in c if pid not in order}

    want = ("new", "fixed", "open") if args.metric == "all" else (args.metric,)
    header = ["Week start (Sun)", "Week end (Sat)"]
    if "new" in want:
        header.append("New")
    for tag in ("fixed", "open"):
        if tag in want:
            header += [f"{tag.capitalize()} {short_priority(labels[pid])}" for pid in order]
            header.append(f"{tag.capitalize()} total")

    rows = []
    for wk in weeks:
        row = [f"{wk:%Y-%m-%d}", f"{wk + timedelta(days=6):%Y-%m-%d}"]
        if "new" in want:
            row.append(str(new_counts.get(wk, 0)))
        for tag, source in (("fixed", fixed), ("open", open_counts)):
            if tag not in want:
                continue
            counts = source.get(wk, blank())
            vals = [counts.get(pid, 0) for pid in order]
            row += [str(v) for v in vals] + [str(sum(vals))]
        rows.append(row)

    if args.csv:
        print(",".join(header))
        for row in rows:
            print(",".join(row))
        return

    width = [max(len(header[c]), *(len(r[c]) for r in rows)) for c in range(len(header))]
    rule = "  " + "  ".join("-" * width[c] for c in range(len(header)))
    print(f"\nBugs per week — {PROJECT} — weeks are Sun-Sat, {tz}")
    print("  " + ",  ".join(labels[pid] for pid in order))
    print("  New and Fixed count the week; Open is the backlog standing at the week's end.\n")
    print("  " + "  ".join(header[c].ljust(width[c]) for c in range(len(header))))
    print(rule)
    for row in rows:
        print("  " + "  ".join(row[c].ljust(width[c]) for c in range(len(header))))
    print(f"\n  {len(bugs)} bugs in {PROJECT}: "
          f"{sum(sum(v.values()) for v in fixed.values())} fixed, {unfixed} still open")
    if partial:
        print(f"  Final row is a partial week; its Open figure is as of "
              f"{now:%Y-%m-%d %H:%M}, not the Saturday.")
    if no_transition:
        print(f"  {len(no_transition)} in a done status with no recorded transition, "
              f"excluded from Fixed: {', '.join(sorted(no_transition))}")
    if stray:
        print(f"  WARNING: priority ids not in the configured scheme: {sorted(stray)}")
    print()


if __name__ == "__main__":
    main()
