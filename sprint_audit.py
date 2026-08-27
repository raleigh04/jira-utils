#!/usr/bin/env python3
"""
Sprint audit — planned vs unplanned completed story points for a Jira sprint.

"Planned"   = the issue was already in the sprint at sprint.startDate.
"Unplanned" = the issue entered the sprint after it started.
"Completed" = the issue's status was in the `done` status category at
              sprint.completeDate (falling back to endDate for a live sprint).

Both determinations replay the issue changelog to reconstruct state at a point
in time, rather than reading only the issue's current state.

Story points are read at their current value; a point value edited after the
sprint closed is reflected here but not in Jira's own sprint report.

Usage:
    python3 sprint_audit.py --list-sprints
    python3 sprint_audit.py --sprint-id 1
    python3 sprint_audit.py --sprint-name "Sprint 1"
"""

import argparse
import os
import re
import sys
from base64 import b64encode
from pathlib import Path
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip3 install requests --break-system-packages")
    sys.exit(1)

ENV_FILE = Path(os.environ.get("JIRA_ENV_FILE") or Path(__file__).with_name(".env"))


def load_env_file(path: Path) -> None:
    """Populate the environment from a KEY=value file. Real env vars win."""
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


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer; got {raw!r} (from {ENV_FILE})")


load_env_file(ENV_FILE)

BASE_URL = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "").strip()
BOARD_ID = env_int("JIRA_BOARD_ID", 1)

STORY_POINT_FIELD_NAMES = ("story points", "story point estimate")
BUG_TYPE_NAMES = ("bug",)
IN_PROGRESS_NAMES = ("in progress",)
IN_REVIEW_NAMES = ("in review",)
TIMEOUT = 30

_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


class Jira:
    def __init__(self, api_token: str):
        credentials = b64encode(f"{EMAIL}:{api_token}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Basic {credentials}", "Accept": "application/json"}
        )

    def get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            sys.exit(
                f"Jira returned {resp.status_code} for {path}. "
                "Check JIRA_API_TOKEN and that the account has access."
            )
        resp.raise_for_status()
        return resp.json()

    def paginate(self, path: str, container: str, **params) -> list:
        """Page through a Jira list endpoint, driven by what the server returns."""
        out, start = [], 0
        while True:
            data = self.get(path, startAt=start, maxResults=100, **params)
            page = data.get(container, [])
            out.extend(page)
            total = data.get("total")
            if not page:
                break
            if data.get("isLast") is True:
                break
            if total is not None and len(out) >= total:
                break
            start += len(page)
        return out


def parse_dt(s: str) -> datetime:
    """Parse a Jira ISO 8601 timestamp (e.g. '2026-06-18T16:07:02.965-0500') as aware UTC."""
    text = s.strip().replace("Z", "+00:00")
    text = _OFFSET_RE.sub(r"\1:\2", text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def issue_sort_key(key: str):
    project, _, num = key.rpartition("-")
    return (project, int(num)) if num.isdigit() else (project, 0)


def fmt_pts(p: float) -> str:
    return f"{p:g}"


# --------------------------------------------------------------------------
# instance metadata
# --------------------------------------------------------------------------

def find_story_point_field(jira: Jira) -> str:
    for field in jira.get("/rest/api/3/field"):
        if field.get("name", "").strip().lower() in STORY_POINT_FIELD_NAMES:
            return field["id"]
    sys.exit("Could not find a 'Story Points' / 'Story point estimate' field on this instance.")


def find_sprint_field(jira: Jira) -> str:
    for field in jira.get("/rest/api/3/field"):
        if field.get("name", "").strip() == "Sprint":
            return field["id"]
    sys.exit("Could not find the 'Sprint' field on this instance.")


def find_statuses_named(jira: Jira, names: tuple) -> set:
    return {str(s["id"]) for s in jira.get("/rest/api/3/status")
            if s["name"].strip().lower() in names}


def find_done_statuses(jira: Jira) -> tuple:
    """Return (set of status ids, set of lowercased names) in the 'done' status category."""
    ids, names = set(), set()
    for status in jira.get("/rest/api/3/status"):
        if status.get("statusCategory", {}).get("key") == "done":
            ids.add(str(status["id"]))
            names.add(status["name"].strip().lower())
    if not ids:
        sys.exit("Could not resolve any 'done' category statuses from /rest/api/3/status.")
    return ids, names


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def list_sprints(jira: Jira) -> list:
    return jira.paginate(f"/rest/agile/1.0/board/{BOARD_ID}/sprint", "values")


def get_sprint(jira: Jira, sprint_id: int) -> dict:
    return jira.get(f"/rest/agile/1.0/sprint/{sprint_id}")


def get_board_issues(jira: Jira, sp_field: str, sprint_field: str) -> list:
    """Every issue on the board, with complete changelogs.

    Deliberately board-wide rather than /sprint/{id}/issue: that endpoint returns
    only issues still attached to the sprint, so work that was committed at sprint
    start and later pulled out is invisible to it — and Jira's velocity report
    still counts it toward commitment.
    """
    issues = jira.paginate(
        f"/rest/agile/1.0/board/{BOARD_ID}/issue",
        "issues",
        fields=f"summary,status,created,issuetype,{sp_field},{sprint_field}",
        expand="changelog",
    )
    for issue in issues:
        changelog = issue.get("changelog", {})
        histories = changelog.get("histories", [])
        if changelog.get("total", len(histories)) > len(histories):
            issue["changelog"] = {"histories": jira.paginate(
                f"/rest/api/3/issue/{issue['key']}/changelog", "values")}
    return issues


def current_sprint_ids(issue: dict, sprint_field: str) -> set:
    return {str(s["id"]) for s in (issue["fields"].get(sprint_field) or [])}


# --------------------------------------------------------------------------
# changelog replay
# --------------------------------------------------------------------------

def changes_for_field(issue: dict, field_id: str = None, field_name: str = None) -> list:
    """Changelog items for one field as [(when, item), ...] in ascending time order."""
    out = []
    for history in issue.get("changelog", {}).get("histories", []):
        when = parse_dt(history["created"])
        for item in history.get("items", []):
            matches_id = field_id is not None and item.get("fieldId") == field_id
            matches_name = field_name is not None and item.get("field") == field_name
            if matches_id or matches_name:
                out.append((when, item))
    out.sort(key=lambda pair: pair[0])
    return out


def sprint_ids(raw) -> set:
    """Parse a Sprint changelog from/to value ('' | '1' | '1, 2, 35') into a set of ids."""
    if not raw:
        return set()
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def was_in_plan(issue: dict, sprint_id: int, sprint_start: datetime, sprint_field: str) -> bool:
    """True if the issue was already in this sprint when the sprint started."""
    if parse_dt(issue["fields"]["created"]) > sprint_start:
        return False  # didn't exist yet — cannot have been planned

    later = [
        (when, item)
        for when, item in changes_for_field(issue, field_name="Sprint")
        if when > sprint_start
    ]
    if later:
        # The `from` of the first post-start change is the membership at sprint start.
        return str(sprint_id) in sprint_ids(later[0][1].get("from"))

    # No sprint change since the sprint started, so membership is unchanged.
    return str(sprint_id) in current_sprint_ids(issue, sprint_field)


def sprint_ids_at_creation(issue: dict, sprint_field: str) -> set:
    """Sprint membership the issue was born with (set at creation writes no changelog)."""
    changes = changes_for_field(issue, field_name="Sprint")
    if changes:
        return sprint_ids(changes[0][1].get("from"))
    return current_sprint_ids(issue, sprint_field)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def estimate_at_creation(issue: dict, sp_field: str) -> float:
    changes = changes_for_field(issue, field_id=sp_field)
    if changes:
        return _num(changes[0][1].get("fromString"))
    return _num(issue["fields"].get(sp_field))


def sprint_scope_change(issue: dict, sprint_id: int, start: datetime, end: datetime,
                        sp_field: str, sprint_field: str) -> tuple:
    """(points added, points removed) for this issue over the sprint window.

    Replays Jira's burndown scope line: every membership change is valued at the
    estimate the issue carried at that instant, and a re-estimate while the issue
    is in the sprint moves scope too. An issue that leaves and returns therefore
    contributes to both sides, at whatever it was worth each time.
    """
    sid = str(sprint_id)
    events = [(w, "sprint", it) for w, it in changes_for_field(issue, field_name="Sprint")]
    events += [(w, "estimate", it) for w, it in changes_for_field(issue, field_id=sp_field)]
    events.sort(key=lambda e: e[0])

    member = sid in sprint_ids_at_creation(issue, sprint_field)
    est = estimate_at_creation(issue, sp_field)
    added = removed = 0.0

    created = parse_dt(issue["fields"]["created"])
    if member and start < created <= end:
        added += est  # created straight into a running sprint

    for when, kind, item in events:
        inside = start < when <= end
        if kind == "sprint":
            now_member = sid in sprint_ids(item.get("to"))
            if inside and now_member != member:
                if now_member:
                    added += est
                else:
                    removed += est
            member = now_member
        else:
            new = _num(item.get("toString"))
            if inside and member:
                delta = new - est
                if delta > 0:
                    added += delta
                else:
                    removed += -delta
            est = new
    return added, removed


def status_timeline(issue: dict) -> list:
    """[(entered_at, status_id)] from creation onward."""
    changes = changes_for_field(issue, field_id="status")
    first = str(changes[0][1].get("from")) if changes else \
        str(issue["fields"].get("status", {}).get("id", ""))
    timeline = [(parse_dt(issue["fields"]["created"]), first)]
    timeline += [(when, str(item.get("to"))) for when, item in changes]
    return timeline


def days_in_statuses(timeline: list, status_ids: set, since: datetime,
                     until: datetime) -> float:
    """Days spent in any of `status_ids`, counting only within [since, until]."""
    total = 0.0
    for idx, (entered, status) in enumerate(timeline):
        left = timeline[idx + 1][0] if idx + 1 < len(timeline) else until
        lo, hi = max(entered, since), min(left, until)
        if hi > lo and status in status_ids:
            total += (hi - lo).total_seconds()
    return total / 86400.0


def cycle_time_days(timeline: list, in_progress: set, done_ids: set,
                    since: datetime, until: datetime):
    """(cycle days, done_at) clamped to [since, until].

    Measured from the first In Progress to the Done that still stood at `until`;
    an earlier Done that was later reopened does not count. A ticket that reached
    Done without ever entering In Progress has a cycle time of zero.
    """
    done_at = None
    for entered, status in timeline:
        if entered > until:
            break
        done_at = entered if status in done_ids else None
    if done_at is None:
        return None, None
    started = next((t for t, st in timeline if st in in_progress and t <= done_at), None)
    if started is None:
        return 0.0, done_at
    span = (done_at - max(started, since)).total_seconds() / 86400.0
    return max(span, 0.0), done_at


def status_id_at(issue: dict, when: datetime) -> str:
    """The issue's status id at a point in time, replayed from the changelog."""
    result = None
    for changed_at, item in changes_for_field(issue, field_id="status"):
        if changed_at <= when:
            result = str(item.get("to"))
        else:
            if result is None:
                result = str(item.get("from"))  # status held before the first later change
            break
    if result is None:
        result = str(issue["fields"].get("status", {}).get("id", ""))
    return result


def points_at(issue: dict, when: datetime, sp_field: str) -> float:
    """The issue's story point estimate at a point in time, replayed from the changelog."""
    value, seen = None, False
    for changed_at, item in changes_for_field(issue, field_id=sp_field):
        if changed_at <= when:
            value, seen = item.get("toString"), True
        else:
            if not seen:
                value, seen = item.get("fromString"), True
            break
    if not seen:
        value = issue["fields"].get(sp_field)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0  # unestimated, or estimate cleared


def was_completed_in_sprint(issue: dict, sprint_end: datetime, done_ids: set) -> bool:
    if parse_dt(issue["fields"]["created"]) > sprint_end:
        return False
    return status_id_at(issue, sprint_end) in done_ids


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def audit_sprint(jira: Jira, sprint: dict):
    sprint_id = sprint["id"]
    if not sprint.get("startDate"):
        sys.exit(f"Sprint '{sprint['name']}' has no startDate (state={sprint.get('state')}) — nothing to audit.")
    sprint_start = parse_dt(sprint["startDate"])
    end_raw = sprint.get("completeDate") or sprint.get("endDate")
    if not end_raw:
        sys.exit(f"Sprint '{sprint['name']}' has neither completeDate nor endDate.")
    sprint_end = parse_dt(end_raw)
    end_label = "completed" if sprint.get("completeDate") else "ends (scheduled)"

    sp_field = find_story_point_field(jira)
    sprint_field = find_sprint_field(jira)
    done_ids, _ = find_done_statuses(jira)
    in_progress_ids = find_statuses_named(jira, IN_PROGRESS_NAMES)
    in_review_ids = find_statuses_named(jira, IN_REVIEW_NAMES)

    print(f"\nSprint: {sprint['name']}  (id={sprint_id}, state={sprint.get('state')})")
    print(f"  started:   {sprint_start:%Y-%m-%d %H:%M UTC}")
    print(f"  {end_label + ':':<10} {sprint_end:%Y-%m-%d %H:%M UTC}")
    print(f"  points field: {sp_field}  (commitment valued at sprint start, completion at sprint close)")
    print("  cycle time: clamped to the sprint window; no In Progress step counts as 0")

    board = get_board_issues(jira, sp_field, sprint_field)
    issues = [i for i in board
              if was_in_plan(i, sprint_id, sprint_start, sprint_field)
              or str(sprint_id) in current_sprint_ids(i, sprint_field)]
    print(f"  issues in scope at any point: {len(issues)}  (scanned {len(board)} board issues)")

    buckets = {("plan", True): [], ("plan", False): [], ("add", True): [], ("add", False): []}
    committed_rows = []
    bugs_fixed = []
    cycles, wip, review = [], [], []
    for issue in issues:
        origin = "plan" if was_in_plan(issue, sprint_id, sprint_start, sprint_field) else "add"
        done = was_completed_in_sprint(issue, sprint_end, done_ids)
        row = (issue["key"], points_at(issue, sprint_end, sp_field),
               issue["fields"].get("summary", ""))
        buckets[(origin, done)].append(row)
        if origin == "plan":
            committed_rows.append(points_at(issue, sprint_start, sp_field))
        if done and issue["fields"]["issuetype"]["name"].strip().lower() in BUG_TYPE_NAMES:
            bugs_fixed.append(row)
        if done:
            timeline = status_timeline(issue)
            cycle, done_at = cycle_time_days(timeline, in_progress_ids, done_ids,
                                             sprint_start, sprint_end)
            if done_at is not None:
                cycles.append(cycle)
                wip.append(days_in_statuses(timeline, in_progress_ids, sprint_start, done_at))
                review.append(days_in_statuses(timeline, in_review_ids, sprint_start, done_at))

    scope_added = scope_removed = 0.0
    added_n = removed_n = 0
    for issue in board:
        up, down = sprint_scope_change(issue, sprint_id, sprint_start, sprint_end,
                                       sp_field, sprint_field)
        scope_added += up
        scope_removed += down
        added_n += bool(up)
        removed_n += bool(down)

    for rows in buckets.values():
        rows.sort(key=lambda r: issue_sort_key(r[0]))

    total = lambda rows: sum(r[1] for r in rows)
    planned_done = buckets[("plan", True)]
    unplanned_done = buckets[("add", True)]
    planned_open = buckets[("plan", False)]
    unplanned_open = buckets[("add", False)]
    completed_points = total(planned_done) + total(unplanned_done)

    def section(title, rows):
        if not rows:
            return
        print(f"\n  {title}  —  {fmt_pts(total(rows))} pts, {len(rows)} tickets")
        for key, pts, summary in rows:
            print(f"    {key:<10} {fmt_pts(pts):>4} pts  {summary[:62]}")

    print(f"\n{'=' * 72}")
    print(f"  {'Committed at sprint start:':<38}{fmt_pts(sum(committed_rows)):>7} pts "
          f"({len(committed_rows)} tickets)")
    print(f"  {'Completed by sprint end:':<38}{fmt_pts(completed_points):>7} pts "
          f"({len(planned_done) + len(unplanned_done)} tickets)")
    print(f"  {'-' * 52}")
    print(f"  {'Completed from the original plan:':<38}{fmt_pts(total(planned_done)):>7} pts "
          f"({len(planned_done)} tickets)")
    print(f"  {'Completed but added mid-sprint:':<38}{fmt_pts(total(unplanned_done)):>7} pts "
          f"({len(unplanned_done)} tickets)")
    print(f"\n  {'Scope added during sprint:':<38}{fmt_pts(scope_added):>7} pts "
          f"({added_n} tickets)")
    print(f"  {'Scope removed during sprint:':<38}{fmt_pts(scope_removed):>7} pts "
          f"({removed_n} tickets)")
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    print(f"\n  {'Average cycle time (In Progress->Done):':<38}{mean(cycles):>7.2f} days "
          f"({len(cycles)} tickets)")
    print(f"  {'  of which, in In Progress:':<38}{mean(wip):>7.2f} days")
    print(f"  {'  of which, in In Review:':<38}{mean(review):>7.2f} days")
    print(f"\n  {'Of that completed work, bugs fixed:':<38}{fmt_pts(total(bugs_fixed)):>7} pts "
          f"({len(bugs_fixed)} tickets)")
    if completed_points:
        print(f"\n  {total(planned_done) / completed_points * 100:.1f}% of completed points came from the original plan.")
    print(f"{'=' * 72}")

    section("PLANNED — completed", planned_done)
    section("UNPLANNED — completed (added after sprint start)", unplanned_done)
    section("PLANNED — not completed", planned_open)
    section("UNPLANNED — not completed", unplanned_open)
    print()


def main():
    parser = argparse.ArgumentParser(description="Jira sprint planned vs unplanned audit")
    parser.add_argument("--list-sprints", action="store_true", help="List sprints on the board")
    parser.add_argument("--sprint-id", type=int, help="Sprint ID to audit")
    parser.add_argument("--sprint-name", type=str, help="Sprint name to audit (substring match)")
    parser.add_argument("--api-token", type=str, help="Jira API token (or set JIRA_API_TOKEN)")
    args = parser.parse_args()

    missing = [n for n, v in (("JIRA_BASE_URL", BASE_URL), ("JIRA_EMAIL", EMAIL)) if not v]
    if missing:
        sys.exit(
            f"Missing required config: {', '.join(missing)}.\n"
            f"Set them in the environment or in {ENV_FILE} "
            f"(see .env.example)."
        )

    api_token = args.api_token or os.environ.get("JIRA_API_TOKEN")
    if not api_token:
        sys.exit(
            f"Missing JIRA_API_TOKEN. Set it in the environment, in {ENV_FILE}, "
            "or pass --api-token."
        )

    jira = Jira(api_token)

    if args.list_sprints:
        sprints = list_sprints(jira)
        print(f"\nSprints on board {BOARD_ID}:")
        for s in sorted(sprints, key=lambda s: s["id"]):
            print(f"  [{s['id']:>4}]  {s['state']:<8}  {s['name']}")
        print()
        return

    if args.sprint_id is not None:
        audit_sprint(jira, get_sprint(jira, args.sprint_id))
        return

    if args.sprint_name:
        matches = [s for s in list_sprints(jira) if args.sprint_name.lower() in s["name"].lower()]
        if not matches:
            sys.exit(f"No sprint matching '{args.sprint_name}'")
        if len(matches) > 1:
            exact = [s for s in matches if s["name"].lower() == args.sprint_name.lower()]
            if len(exact) != 1:
                print("Multiple matches — be more specific:")
                for s in matches:
                    print(f"  [{s['id']}] {s['name']}")
                sys.exit(1)
            matches = exact
        audit_sprint(jira, matches[0])
        return

    parser.print_help()


if __name__ == "__main__":
    main()
