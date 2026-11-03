#!/usr/bin/env python3
"""Stage 1 V2: auditably screen GitHub users for sustained activity.

Criterion (preserved from V1): at least MIN_COMMITS distinct GitHub-attributed
commit SHAs in each of PANELS consecutive complete calendar half-years,
independently before and after the cutoff.

Important measurement limitation: this script uses GitHub Commit Search. Search
results are an indexed, access-dependent view of GitHub, not a complete record
of a developer's historical activity. A completed search only supports claims
inside this explicitly defined measurement scope.
"""
import argparse
import csv
import json
import hashlib
from pathlib import Path
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests

BASE = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "research-active-github-screen/3.0",
}

ACTIVE_FIELDS = [
    "username",
    "profile_url",
    "before_eligibility",
    "after_eligibility",
    "before_qualifying_periods",
    "after_qualifying_periods",
    "status",
    "notes",
]

PERIOD_FIELDS = [
    "username",
    "period_start",
    "period_end",
    "observed_verified_commits",
    "github_total_count",
    "incomplete_results",
    "pages_retrieved",
    "pagination_limit_reached",
    "threshold_confirmed",
    "inconclusive",
    "period_state",
    "notes",
]


@dataclass
class PeriodEvidence:
    username: str
    period_start: date
    period_end: date  # inclusive date for CSV readability
    observed_verified_commits: int = 0
    github_total_count: Optional[int] = None
    incomplete_results: bool = False
    pages_retrieved: int = 0
    pagination_limit_reached: bool = False
    threshold_confirmed: bool = False
    inconclusive: bool = False
    period_state: str = "INCONCLUSIVE"  # PASS / FAIL / INCONCLUSIVE / ERROR
    notes: str = ""

    def csv_row(self) -> dict:
        row = asdict(self)
        row["period_start"] = self.period_start.isoformat()
        row["period_end"] = self.period_end.isoformat()
        row["github_total_count"] = "" if self.github_total_count is None else self.github_total_count
        row["incomplete_results"] = yes_no(self.incomplete_results)
        row["pagination_limit_reached"] = yes_no(self.pagination_limit_reached)
        row["threshold_confirmed"] = yes_no(self.threshold_confirmed)
        row["inconclusive"] = yes_no(self.inconclusive)
        return row


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def half_start(d: date) -> date:
    return date(d.year, 1 if d.month <= 6 else 7, 1)


def next_half(d: date) -> date:
    return date(d.year + (d.month == 7), 1 if d.month == 7 else 7, 1)


def complete_panels(start: date, end_exclusive: date) -> List[Tuple[date, date]]:
    """Return only full Jan-Jun or Jul-Dec intervals inside [start, end)."""
    d = half_start(start)
    if d < start:
        d = next_half(d)
    output = []
    while next_half(d) <= end_exclusive:
        output.append((d, next_half(d)))
        d = next_half(d)
    return output


def request(session: requests.Session, path: str, params: dict) -> dict:
    """GET JSON with the V1 retry/rate-limit behavior preserved."""
    for retry in range(4):
        try:
            r = session.get(BASE + path, params=params, timeout=40)
        except requests.RequestException as exc:
            if retry == 3:
                raise RuntimeError(f"Network failure: {exc}") from exc
            time.sleep(2 ** retry)
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code in (403, 429):
            remaining = r.headers.get("X-RateLimit-Remaining")
            reset = r.headers.get("X-RateLimit-Reset")
            retry_after = r.headers.get("Retry-After")
            if remaining == "0" and reset:
                wait = max(2, int(reset) - int(time.time()) + 2)
            else:
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 5 * (retry + 1)
            if wait > 120:
                raise RuntimeError(f"Rate limit requires waiting {wait}s; rerun later")
            print(f"  API throttled: sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if r.status_code in (404, 422):
            raise RuntimeError(f"API {r.status_code}: {r.text[:180]}")
        if 500 <= r.status_code < 600 and retry < 3:
            time.sleep(2 ** retry)
            continue
        raise RuntimeError(f"API {r.status_code}: {r.text[:200]}")

    raise RuntimeError("API retries exhausted")


def discover(session: requests.Session, limit: int, search_pages: int) -> List[str]:
    """Convenience discovery only; not a random or representative sample."""
    users: Dict[str, str] = {}
    for page in range(1, search_pages + 1):
        result = request(session, "/search/repositories", {
            "q": "fork:false archived:false",
            "sort": "updated",
            "order": "desc",
            "per_page": 100,
            "page": page,
        })
        for repo in result.get("items", []):
            owner = repo.get("owner") or {}
            if owner.get("type") == "User" and owner.get("login"):
                users[owner["login"].casefold()] = owner["login"]
        if len(users) >= limit or not result.get("items"):
            break
    return list(users.values())[:limit]


def count_period(
    session: requests.Session,
    username: str,
    start: date,
    stop: date,
    threshold: int,
    max_pages: int,
) -> PeriodEvidence:
    """Collect auditable evidence for one half-year.

    PASS is established as soon as threshold distinct SHAs with author.login equal
    to username are observed.

    FAIL is used only when the available search evidence itself rules out reaching
    threshold inside this Commit Search measurement. Incomplete or truncated
    evidence becomes INCONCLUSIVE rather than zero/failure.
    """
    evidence = PeriodEvidence(
        username=username,
        period_start=start,
        period_end=stop - timedelta(days=1),
    )
    end_inclusive = stop - timedelta(days=1)
    query = (
        f"author:{username} author-date:{start.isoformat()}.."
        f"{end_inclusive.isoformat()}"
    )

    seen = set()
    first_total: Optional[int] = None
    any_incomplete = False

    try:
        for page in range(1, max_pages + 1):
            result = request(session, "/search/commits", {
                "q": query,
                "per_page": 100,
                "page": page,
            })
            evidence.pages_retrieved += 1

            total = result.get("total_count")
            if first_total is None and isinstance(total, int):
                first_total = total
                evidence.github_total_count = total

            incomplete = bool(result.get("incomplete_results", False))
            any_incomplete = any_incomplete or incomplete
            evidence.incomplete_results = any_incomplete

            items = result.get("items", []) or []
            for item in items:
                author = item.get("author") or {}
                if (author.get("login") or "").casefold() != username.casefold():
                    continue
                sha = item.get("sha")
                if sha:
                    seen.add(sha)

            evidence.observed_verified_commits = len(seen)

            # Positive evidence is sufficient even if GitHub labels the query incomplete.
            if len(seen) >= threshold:
                evidence.threshold_confirmed = True
                evidence.inconclusive = False
                evidence.period_state = "PASS"
                evidence.notes = (
                    "Threshold confirmed from distinct observed SHAs; retrieval stopped early "
                    "once the threshold was reached. Observed count is a lower bound."
                )
                return evidence

            # If GitHub says the search may be incomplete, do not infer a negative.
            if incomplete:
                evidence.inconclusive = True
                evidence.period_state = "INCONCLUSIVE"
                evidence.notes = (
                    "GitHub returned incomplete_results=true before the threshold was confirmed; "
                    "a negative conclusion is not supported."
                )
                return evidence

            # If total_count itself is below threshold, the current completed search result
            # cannot contain enough matches to satisfy the criterion. For threshold <=100,
            # the first page also contains the complete matching set; for larger thresholds,
            # we intentionally do not fetch extra pages because total_count already rules out PASS.
            if isinstance(total, int) and total < threshold:
                evidence.inconclusive = False
                evidence.period_state = "FAIL"
                evidence.notes = (
                    "Completed Commit Search reported total_count below the threshold. "
                    "This is a conclusive failure only within the defined Commit Search measurement scope."
                )
                return evidence

            # Exhausted all reported results before threshold.
            if not items or (isinstance(total, int) and page * 100 >= total):
                evidence.inconclusive = False
                evidence.period_state = "FAIL"
                evidence.notes = (
                    "All reported accessible search results were retrieved without confirming the threshold. "
                    "This does not prove complete historical GitHub activity was observed."
                )
                return evidence

        # We used every allowed page and still did not have enough evidence either way.
        evidence.pagination_limit_reached = True
        evidence.inconclusive = True
        evidence.period_state = "INCONCLUSIVE"
        evidence.notes = (
            "Configured pagination limit was reached before the threshold was confirmed or the "
            "reported result set was exhausted."
        )
        return evidence

    except RuntimeError as exc:
        evidence.observed_verified_commits = len(seen)
        evidence.incomplete_results = any_incomplete
        evidence.inconclusive = True
        evidence.period_state = "ERROR"
        evidence.notes = str(exc)
        return evidence


def qualifying_sequences(
    panels: Sequence[Tuple[date, date]],
    results: Dict[date, PeriodEvidence],
    needed: int,
) -> List[str]:
    """Return all consecutive sequences whose every period is PASS."""
    sequences: List[str] = []
    for i in range(len(panels) - needed + 1):
        window = panels[i:i + needed]
        if all(results[p[0]].period_state == "PASS" for p in window):
            sequences.append(" | ".join(f"{p[0].isoformat()}..{(p[1]-timedelta(days=1)).isoformat()}" for p in window))
    return sequences


def classify_era(
    panels: Sequence[Tuple[date, date]],
    results: Dict[date, PeriodEvidence],
    needed: int,
) -> Tuple[str, List[str]]:
    """Return PASS, FAIL, REVIEW, or ERROR for one observation era.

    FAIL is allowed only when every possible consecutive window contains at least
    one conclusive FAIL period. Therefore no unresolved period could rescue any
    possible qualifying window.
    """
    sequences = qualifying_sequences(panels, results, needed)
    if sequences:
        return "PASS", sequences

    windows = [panels[i:i + needed] for i in range(len(panels) - needed + 1)]
    if not windows:
        return "ERROR", []

    if any(any(results[p[0]].period_state == "ERROR" for p in window) for window in windows):
        # An ERROR matters only if there remains a window not already ruled out by FAIL.
        unresolved_error_window = any(
            not any(results[p[0]].period_state == "FAIL" for p in window)
            and any(results[p[0]].period_state == "ERROR" for p in window)
            for window in windows
        )
        if unresolved_error_window:
            return "ERROR", []

    definitely_fails = all(
        any(results[p[0]].period_state == "FAIL" for p in window)
        for window in windows
    )
    if definitely_fails:
        return "FAIL", []

    return "REVIEW", []


def final_status(before: str, after: str) -> str:
    if before == "PASS" and after == "PASS":
        return "CANDIDATE"
    # One conclusively failed era is already sufficient to rule out candidacy;
    # an error in the other era does not undo that negative conclusion.
    if "FAIL" in (before, after):
        return "INSUFFICIENT_ACTIVITY"
    if "ERROR" in (before, after):
        return "ERROR"
    return "REVIEW"


def era_label(state: str) -> str:
    return {
        "PASS": "yes",
        "FAIL": "no",
        "REVIEW": "review",
        "ERROR": "error",
    }[state]


def summarize_limitations(periods: Iterable[PeriodEvidence]) -> str:
    periods = list(periods)
    inconclusive = sum(p.period_state == "INCONCLUSIVE" for p in periods)
    errors = sum(p.period_state == "ERROR" for p in periods)
    limited = sum(p.pagination_limit_reached for p in periods)
    incomplete = sum(p.incomplete_results for p in periods)
    parts = [
        "Measurement uses GitHub Commit Search over public/access-permitted indexed results and verifies author.login; it is not a complete historical activity log.",
        "Commit Search is centered on commits found via GitHub's search index/default-branch view; private/inaccessible repositories, unmerged branches, deleted repositories, and identities not associated with the account can be missing.",
        "PASS counts stop once the threshold is confirmed, so observed counts may be lower bounds.",
    ]
    if inconclusive:
        parts.append(f"{inconclusive} period(s) were inconclusive.")
    if errors:
        parts.append(f"{errors} period(s) had technical errors.")
    if limited:
        parts.append(f"{limited} period(s) hit the configured pagination limit.")
    if incomplete:
        parts.append(f"{incomplete} period(s) returned incomplete_results=true.")
    return " ".join(parts)


def assess(session: requests.Session, user: str, args, cache=None, save_period=None) -> Tuple[dict, List[PeriodEvidence]]:
    """Evaluate every panel; completed cached panels require no API call."""
    cache = cache or {}
    profile_url = f"https://github.com/{user}"
    periods: List[PeriodEvidence] = []

    pre_panels = complete_panels(args.before_start, args.cutoff)
    post_panels = complete_panels(args.cutoff, args.after_end + timedelta(days=1))
    if min(len(pre_panels), len(post_panels)) < args.panels:
        return error_row(user, "Observation windows are too short"), periods

    results: Dict[date, PeriodEvidence] = {}
    for start, stop in pre_panels + post_panels:
        key = (user.casefold(), start)
        old = cache.get(key)
        reusable = old and old.period_end == stop - timedelta(days=1) and (old.period_state in ("PASS", "FAIL") or (old.period_state == "INCONCLUSIVE" and not args.retry_inconclusive))
        if reusable:
            evidence = old
            action = "CACHED"
        else:
            evidence = count_period(session, user, start, stop, args.min_commits, args.max_search_pages)
            action = "FETCHED"
            if save_period is not None:
                save_period(evidence)
            if args.delay:
                time.sleep(args.delay)
        periods.append(evidence)
        results[start] = evidence
        print(f"  {start} to {stop - timedelta(days=1)}: {evidence.period_state} "
              f"({evidence.observed_verified_commits} SHAs; pages={evidence.pages_retrieved}; {action})", file=sys.stderr)

    before_state, before_sequences = classify_era(pre_panels, results, args.panels)
    after_state, after_sequences = classify_era(post_panels, results, args.panels)
    row = {
        "username": user,
        "profile_url": profile_url,
        "before_eligibility": era_label(before_state),
        "after_eligibility": era_label(after_state),
        "before_qualifying_periods": "; ".join(before_sequences),
        "after_qualifying_periods": "; ".join(after_sequences),
        "status": final_status(before_state, after_state),
        "notes": summarize_limitations(periods),
    }
    return row, periods


def error_row(user, message):
    return {
        "username": user, "profile_url": f"https://github.com/{user}",
        "before_eligibility": "error", "after_eligibility": "error",
        "before_qualifying_periods": "", "after_qualifying_periods": "",
        "status": "ERROR", "notes": message,
    }


def parse():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed-user", action="append", default=[])
    p.add_argument("--users-file", help="One username per line, or CSV with username column")
    p.add_argument("--max-users", type=int, default=10)
    p.add_argument("--search-pages", type=int, default=2)
    p.add_argument("--max-search-pages", type=int, default=10)
    p.add_argument("--min-commits", type=int, default=100)
    p.add_argument("--panels", type=int, default=3)
    p.add_argument("--before-start", type=date.fromisoformat, default=date(2019, 1, 1))
    p.add_argument("--cutoff", type=date.fromisoformat, default=date(2023, 1, 1))
    p.add_argument("--after-end", type=date.fromisoformat, default=date(2026, 10, 31))
    p.add_argument("--delay", type=float, default=2.2)
    p.add_argument("--resume", action="store_true", help="Reuse completed periods from a compatible checkpoint")
    p.add_argument("--adopt-existing", action="store_true", help="Explicitly trust a pre-V3 activity_periods.csv without run metadata, after manual settings verification")
    p.add_argument("--retry-inconclusive", action="store_true", help="Retry INCONCLUSIVE as well as ERROR periods (normally only ERROR is retried)")
    p.add_argument("--output", default="active_users.csv", help="Developer-level output CSV")
    p.add_argument("--period-output", default="activity_periods.csv", help="Period-level output CSV")
    args = p.parse_args()

    if not (args.before_start < args.cutoff <= args.after_end):
        p.error("Invalid observation windows")
    if min(args.max_users, args.search_pages, args.max_search_pages, args.min_commits, args.panels) < 1:
        p.error("Counts and limits must be positive")
    if args.max_search_pages > 10 or args.min_commits > 1000:
        p.error("max-search-pages <= 10 and min-commits <= 1000 required")
    if args.adopt_existing and not args.resume:
        p.error("--adopt-existing requires --resume")
    if args.retry_inconclusive and not args.resume:
        p.error("--retry-inconclusive requires --resume")
    if Path(args.output).resolve() == Path(args.period_output).resolve():
        p.error("--output and --period-output must be different files")
    return args


def load_users(args, session: requests.Session) -> List[str]:
    users = list(args.seed_user)
    if args.users_file:
        with open(args.users_file, encoding="utf-8-sig", newline="") as f:
            if args.users_file.lower().endswith(".csv"):
                users.extend(r["username"] for r in csv.DictReader(f) if r.get("username"))
            else:
                users.extend(line.strip() for line in f if line.strip())
    if not users:
        users = discover(session, args.max_users, args.search_pages)
    return list(dict.fromkeys(u.strip() for u in users if u.strip()))[:args.max_users]


def config(args):
    """Inputs that change period data or the interpretation of screening."""
    return {
        "version": 3,
        "before_start": args.before_start.isoformat(),
        "cutoff": args.cutoff.isoformat(),
        "after_end": args.after_end.isoformat(),
        "min_commits": args.min_commits,
        "panels": args.panels,
        "max_search_pages": args.max_search_pages,
        "search_method": "github_commit_search_author_author-date_distinct_sha_author_login_default-branch",
    }


def save_csv_atomic(path, fields, rows):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def save_json_atomic(path, data):
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def parse_evidence(row):
    def boolean(value):
        if value not in ("yes", "no"):
            raise ValueError(f"Invalid boolean value {value!r}")
        return value == "yes"
    state = row["period_state"]
    if state not in ("PASS", "FAIL", "INCONCLUSIVE", "ERROR"):
        raise ValueError(f"Invalid period state {state!r}")
    return PeriodEvidence(
        username=row["username"], period_start=date.fromisoformat(row["period_start"]),
        period_end=date.fromisoformat(row["period_end"]),
        observed_verified_commits=int(row["observed_verified_commits"]),
        github_total_count=(int(row["github_total_count"]) if row["github_total_count"] != "" else None),
        incomplete_results=boolean(row["incomplete_results"]),
        pages_retrieved=int(row["pages_retrieved"]),
        pagination_limit_reached=boolean(row["pagination_limit_reached"]),
        threshold_confirmed=boolean(row["threshold_confirmed"]),
        inconclusive=boolean(row["inconclusive"]), period_state=state,
        notes=row.get("notes", ""),
    )


def load_checkpoint(args):
    path = Path(args.period_output)
    metadata = Path(str(path) + ".meta.json")
    expected = config(args)
    saved = {}
    if not args.resume:
        return saved, metadata
    if not path.exists():
        if metadata.exists():
            raise SystemExit("Checkpoint metadata exists but period CSV is missing; refusing unsafe resume")
        print("No checkpoint found; beginning a new run", file=sys.stderr)
        return saved, metadata
    if metadata.exists():
        try:
            actual = json.loads(metadata.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SystemExit(f"Cannot read checkpoint metadata: {exc}")
        if actual != expected:
            raise SystemExit(f"Saved checkpoint parameters do not match this run. Existing: {actual}; requested: {expected}. Use new output paths for different settings.")
    elif not args.adopt_existing:
        raise SystemExit("Existing period CSV has no V3 metadata. Rerun with --resume --adopt-existing ONLY after confirming it used the same dates, threshold, panels and max-search-pages; otherwise use new output paths.")
    else:
        print("WARNING: adopting a legacy CSV. Its collection settings cannot be verified automatically.", file=sys.stderr)
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not set(PERIOD_FIELDS).issubset(reader.fieldnames or []):
                raise ValueError("Missing required activity_periods.csv columns")
            for row in reader:
                evidence = parse_evidence(row)
                key = (evidence.username.casefold(), evidence.period_start)
                if key in saved:
                    raise ValueError(f"Duplicate checkpoint period: {key}")
                saved[key] = evidence
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Cannot safely resume checkpoint: {exc}")
    # Confirm every cached period is part of the current observation windows.
    valid = {start: stop - timedelta(days=1) for start, stop in
             complete_panels(args.before_start, args.cutoff) +
             complete_panels(args.cutoff, args.after_end + timedelta(days=1))}
    for (user, start), evidence in saved.items():
        if start not in valid or evidence.period_end != valid[start]:
            raise SystemExit(f"Checkpoint contains incompatible period for {user}: {start}")
    print(f"Loaded {len(saved)} saved period results", file=sys.stderr)
    return saved, metadata


def main():
    args = parse()
    saved, metadata = load_checkpoint(args)
    # Guard against overwriting a resume checkpoint if authentication is missing.
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN before running (GitHub API authentication required).")
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = f"Bearer {token}"
    users = load_users(args, session)
    # Store any old checkpoint rows, including other users, when resuming.
    period_cache = dict(saved)
    active_rows = {}
    if args.resume and Path(args.output).exists():
        with open(args.output, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if set(ACTIVE_FIELDS).issubset(reader.fieldnames or []):
                active_rows = {r["username"].casefold(): r for r in reader if r.get("username")}

    # Initial metadata is written only after the saved checkpoint has passed validation.
    save_json_atomic(metadata, config(args))

    def persist_period(evidence):
        period_cache[(evidence.username.casefold(), evidence.period_start)] = evidence
        rows = [e.csv_row() for _, e in sorted(period_cache.items(), key=lambda t: t[0])]
        save_csv_atomic(args.period_output, PERIOD_FIELDS, rows)

    for i, username in enumerate(users, 1):
        print(f"[{i}/{len(users)}] Checking {username}", file=sys.stderr)
        # The profile was verified by V2 if adopting a legacy run. New accounts
        # must still be checked as individual accounts before querying activity.
        if not any(key[0] == username.casefold() for key in saved):
            try:
                profile = request(session, f"/users/{username}", {})
                if profile.get("type") != "User":
                    raise RuntimeError("Account is not an individual GitHub User")
            except RuntimeError as exc:
                active_rows[username.casefold()] = error_row(username, str(exc))
                save_csv_atomic(args.output, ACTIVE_FIELDS, list(active_rows.values()))
                print(f"  => ERROR ({exc})", file=sys.stderr)
                continue
        row, _ = assess(session, username, args, period_cache, persist_period)
        active_rows[username.casefold()] = row
        save_csv_atomic(args.output, ACTIVE_FIELDS, list(active_rows.values()))
        print(f"  => {row['status']}", file=sys.stderr)
    # Ensure output CSV exists even if no period data was fetched.
    save_csv_atomic(args.period_output, PERIOD_FIELDS,
                    [e.csv_row() for _, e in sorted(period_cache.items(), key=lambda t: t[0])])
    save_csv_atomic(args.output, ACTIVE_FIELDS, list(active_rows.values()))
    print("Wrote", args.output)
    print("Wrote", args.period_output)
    print("Checkpoint settings:", metadata)


if __name__ == "__main__":
    main()
