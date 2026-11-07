#!/usr/bin/env python3
"""collect.py - screen candidates (from discover_pronouns.py), then select equal samples.
Usage:
  python collect.py screen --target 20      # until EVERY country x gender cell has 20 passes
  python collect.py select --quota 15       # equal feminine/masculine per country
Env: GITHUB_TOKEN, STUDY_SALT (keep the salt fixed for the whole project)
"""
import argparse, csv, hashlib, os, random, re, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import requests

# CONFIG ------------------------------------------------------------------------------------------
UTC = timezone.utc
WINDOWS = {  # no gap: post starts on the day after 30 Nov 2022
    "pre":  (datetime(2021, 1, 1, tzinfo=UTC), datetime(2022, 11, 29, 23, 59, 59, tzinfo=UTC)),
    "post": (datetime(2022, 11, 30, tzinfo=UTC), datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)),
}
# eligibility thresholds, applied to EACH window and identical for every country/gender
MIN_COMMITS = 5         # user's Python commits (after bulk filter)
MIN_WEEKS = 2           # distinct active ISO weeks
MIN_FILES = 3           # distinct Python files
BULK_THRESHOLD = 30     # ignore commits touching more .py files than this
MIN_AUTHOR_SHARE = 0.90 # user's share of all Python commits in a repo
MAX_REPOS = 8           # repos inspected per user
MAX_REPO_KB = 150_000   # skip very large repos
CLONE_TIMEOUT = 300     # seconds
DELAY = 0.5             # seconds between API calls

EXCLUDE_PATH = re.compile(
    r"(^|/)(vendor|vendored|third_party|site-packages|node_modules|venv|\.venv|env|"
    r"build|dist|migrations|docs?|examples?|tutorials?)(/|$)|_pb2(_grpc)?\.py$|"
    r"(^|/)(setup|conftest)\.py$|(^|/)__init__\.py$", re.I)
EXCLUDE_REPO = re.compile(
    r"tutorial|course|homework|assignment|bootcamp|leetcode|exercise|cheatsheet|"
    r"template|boilerplate|learn", re.I)

class GitError(RuntimeError):
    pass

def log(msg):
    print(msg, file=sys.stderr, flush=True)

def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))

def uid_for(login):
    salt = os.environ.get("STUDY_SALT")
    if not salt:
        raise SystemExit("Set STUDY_SALT (any secret string) before running.")
    return hashlib.sha256((salt + login.lower()).encode()).hexdigest()[:10]

def run(cmd, timeout=900):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never hang on credential prompts
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"timeout: {' '.join(cmd[:4])}") from exc

def git(repo, *args, timeout=900):
    r = run(["git", "-C", str(repo), *args], timeout)
    if r.returncode:
        raise GitError(r.stderr.strip()[:300] or "git error")
    return r.stdout

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "llm-style-research"})
if os.environ.get("GITHUB_TOKEN"):
    SESSION.headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

def api_get(path, params=None, retries=5):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            time.sleep(DELAY)
            return r.json()
        if r.status_code in (403, 429):
            if r.headers.get("X-RateLimit-Remaining") == "0":
                wait = max(int(r.headers.get("X-RateLimit-Reset", 0)) - time.time(), 5) + 2
            else:
                wait = int(r.headers.get("Retry-After", 30))
            log(f"  rate limited, sleeping {int(wait)}s")
            time.sleep(min(wait, 3700))
            continue
        if r.status_code in (404, 409, 422):   # missing / empty repo / bad query
            return None
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    raise GitError(f"API failed: {path}")

# REPOSITORY ANALYSIS -----------------------------------------------------------------------------
def clone(url, dest):
    if dest.exists():
        git(dest, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*")
    else:
        r = run(["git", "clone", "--bare", "--quiet", url, str(dest)], timeout=CLONE_TIMEOUT)
        if r.returncode:
            raise GitError(r.stderr.strip()[:300])

def python_commits(repo):
    """Non-merge commits on HEAD that add/modify .py files."""
    out = git(repo, "log", "HEAD", "--no-merges", "--no-renames", "--diff-filter=AM",
              "--name-only", "--format=%x1e%H%x1f%aI%x1f%ae", "--", "*.py")
    commits = []
    for chunk in out.split("\x1e")[1:]:
        lines = chunk.splitlines()
        h, d, e = lines[0].split("\x1f")
        files = [l.strip() for l in lines[1:] if l.strip().endswith(".py")]
        if files:
            commits.append({"hash": h, "date": datetime.fromisoformat(d).astimezone(UTC),
                            "email": e.strip().lower(), "files": files})
    return commits

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def eligible_repos(login):
    repos, page = [], 1
    while page <= 5:
        data = api_get(f"/users/{login}/repos",
                       {"type": "owner", "sort": "pushed", "per_page": 100, "page": page})
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    keep = [r for r in repos
            if not r["fork"] and not r["archived"] and not r.get("disabled")
            and 0 < r["size"] < MAX_REPO_KB and r.get("language") == "Python"
            and (r.get("pushed_at") or "") >= "2021-01-01"
            and not EXCLUDE_REPO.search(r["name"] + " " + (r.get("description") or ""))]
    return keep[:MAX_REPOS]

def verified_emails(profile, repos):
    """Emails GitHub itself links to this login (API author-login match), plus per-repo
    commit counts per window (all file types, so an UPPER bound on Python commits;
    max 100 per query). Never inferred from display names."""
    login = profile["login"]
    emails = {f"{profile['id']}+{login}@users.noreply.github.com".lower(),
              f"{login}@users.noreply.github.com".lower()}
    counts = {}
    for r in repos:
        counts[r["full_name"]] = {}
        for w, (lo, hi) in WINDOWS.items():
            data = api_get(f"/repos/{r['full_name']}/commits",
                           {"author": login, "since": iso(lo), "until": iso(hi),
                            "per_page": 100}) or []
            mine = [c for c in data
                    if ((c.get("author") or {}).get("login") or "").lower() == login.lower()]
            counts[r["full_name"]][w] = len(mine)
            for c in mine:
                e = ((c.get("commit") or {}).get("author") or {}).get("email")
                if e:
                    emails.add(e.lower())
    return emails, counts

def snapshot(repo_dir, emails):
    """Per window: user's commit count, active weeks, and the latest version (commit hash)
    of each eligible file. None if the repo fails the single-author rule."""
    commits = python_commits(repo_dir)
    if not commits:
        return None
    mine = [c for c in commits if c["email"] in emails]
    if len(mine) / len(commits) < MIN_AUTHOR_SHARE:
        return None
    result = {}
    for w, (lo, hi) in WINDOWS.items():
        latest, n, weeks = {}, 0, set()
        for c in mine:
            if not (lo <= c["date"] <= hi) or len(c["files"]) > BULK_THRESHOLD:
                continue
            files = [f for f in c["files"] if not EXCLUDE_PATH.search(f)]
            if not files:
                continue
            n += 1
            weeks.add(c["date"].strftime("%G-W%V"))
            for f in files:
                if f not in latest or c["date"] > latest[f][1]:
                    latest[f] = (c["hash"], c["date"])
        result[w] = {"commits": n, "weeks": weeks,
                     "files": {p: h for p, (h, _) in latest.items()}}
    return result

# SCREENING USERS, PER COUNTRY X GENDER CELL ------------------------------------------------------
LOG_FIELDS = ["uid", "login", "country", "group", "outcome", "repos_used",
              "pre_commits", "pre_weeks", "pre_files",
              "post_commits", "post_weeks", "post_files"]
MANIFEST_FIELDS = ["uid", "period", "repo", "path", "commit"]

def screen_user(login, cache):
    """Returns (outcome, stats_dict, manifest_rows)."""
    profile = api_get(f"/users/{login}")
    if not profile or profile.get("type") != "User":
        return "NOT_USER", {}, []
    repos = eligible_repos(login)
    if not repos:
        return "NO_REPOS", {}, []
    emails, counts = verified_emails(profile, repos)
    for w in WINDOWS:   # cheap rejection before any cloning
        if sum(c[w] for c in counts.values()) < MIN_COMMITS:
            return f"PRESCREEN_THIN_{w.upper()}", {}, []
    repos = [r for r in repos if any(counts[r["full_name"]].values())]

    win = {w: {"commits": 0, "weeks": set(), "rows": []} for w in WINDOWS}
    used = 0
    for r in repos:
        dest = cache / f"{r['full_name'].replace('/', '__')}.git"
        log(f"    cloning {r['name']}")
        try:
            clone(r["clone_url"], dest)
            snap = snapshot(dest, emails)
        except (GitError, ValueError) as exc:
            log(f"    skip {r['name']}: {exc}")
            continue
        if not snap:
            continue
        used += 1
        for w in WINDOWS:
            win[w]["commits"] += snap[w]["commits"]
            win[w]["weeks"] |= snap[w]["weeks"]
            win[w]["rows"] += [(r["full_name"], p, h) for p, h in snap[w]["files"].items()]
    stats = {"repos_used": used}
    for w in WINDOWS:
        stats[f"{w}_commits"] = win[w]["commits"]
        stats[f"{w}_weeks"] = len(win[w]["weeks"])
        stats[f"{w}_files"] = len(win[w]["rows"])
    for w in WINDOWS:
        if (win[w]["commits"] < MIN_COMMITS or len(win[w]["weeks"]) < MIN_WEEKS
                or len(win[w]["rows"]) < MIN_FILES):
            return f"THIN_{w.upper()}", stats, []
    uid = uid_for(login)
    rows = [{"uid": uid, "period": w, "repo": repo, "path": p, "commit": h}
            for w in WINDOWS for repo, p, h in win[w]["rows"]]
    return "OK", stats, rows

def cmd_screen(args):
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    cands = read_csv(args.candidates)
    random.Random(args.seed).shuffle(cands)
    log_path, man_path = Path(args.log), Path(args.manifest)

    done, ok = set(), defaultdict(int)
    if log_path.exists():
        for r in read_csv(log_path):
            done.add(r["login"])
            if r["outcome"] == "OK":
                ok[(r["country"], r["group"])] += 1
    log_new = not log_path.exists() or log_path.stat().st_size == 0
    man_new = not man_path.exists() or man_path.stat().st_size == 0
    lf = open(log_path, "a", newline="", encoding="utf-8")
    lw = csv.DictWriter(lf, fieldnames=LOG_FIELDS)
    if log_new:
        lw.writeheader()
    mf = open(man_path, "a", newline="", encoding="utf-8")
    mw = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS)
    if man_new:
        mw.writeheader()

    cells = {(c["country"], c["group"].strip().lower()) for c in cands}
    for c in cands:
        if all(ok[cell] >= args.target for cell in cells):
            break
        login, country, g = c["login"], c["country"], c["group"].strip().lower()
        if login in done or ok[(country, g)] >= args.target:
            continue
        status = " ".join(f"{k[0][:2]}-{k[1][:1]}:{ok[k]}" for k in sorted(cells))
        log(f"[{status} / {args.target}] {login} ({country}, {g})")
        try:
            outcome, stats, rows = screen_user(login, cache)
        except Exception as exc:  # keep long runs alive
            outcome, stats, rows = f"ERROR:{type(exc).__name__}", {}, []
        log(f"   -> {outcome}")
        lw.writerow({"uid": uid_for(login), "login": login, "country": country,
                     "group": g, "outcome": outcome, **stats})
        mw.writerows(rows)
        lf.flush(); mf.flush()
        if outcome == "OK":
            ok[(country, g)] += 1
    log(f"Done. Passes per (country, group): {dict(ok)}. Log/manifest are PRIVATE.")

# SELECTION AND CLI -------------------------------------------------------------------------------
def cmd_select(args):
    """Within EACH country take the same number of feminine and masculine users:
    n = min(feminine passes, masculine passes, quota). Random, never by metric values."""
    cells = defaultdict(list)
    for r in read_csv(args.log):
        if r["outcome"] == "OK":
            cells[(r["country"], r["group"])].append(r["uid"])
    rnd = random.Random(args.seed)
    out = []
    for country in sorted({c for c, _ in cells}):
        f, m = cells[(country, "feminine")], cells[(country, "masculine")]
        n = min(len(f), len(m), args.quota)
        print(f"{country}: passed screening feminine {len(f)}, masculine {len(m)} "
              f"-> taking {n} of each (quota {args.quota})")
        if n < args.quota:
            print(f"  SHORT: screen more {'feminine' if len(f) <= len(m) else 'masculine'} "
                  f"candidates for {country}")
        rnd.shuffle(f); rnd.shuffle(m)
        out += [{"uid": u, "gender": "feminine", "country": country} for u in f[:n]]
        out += [{"uid": u, "gender": "masculine", "country": country} for u in m[:n]]
    with open("selected_users.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["uid", "gender", "country"])
        w.writeheader()
        w.writerows(out)
    print(f"Wrote selected_users.csv ({len(out)} users). uids only: safe for metrics.py.")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("screen")
    s.add_argument("--candidates", default="pronoun_users.csv")
    s.add_argument("--target", type=int, default=20,
                   help="stop when EVERY country x gender cell has this many passes")
    s.add_argument("--cache", default="repo_cache")
    s.add_argument("--log", default="screen_log.csv")
    s.add_argument("--manifest", default="manifest.csv")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(fn=cmd_screen)
    t = sub.add_parser("select")
    t.add_argument("--log", default="screen_log.csv")
    t.add_argument("--quota", type=int, default=15, help="max users per gender PER COUNTRY")
    t.add_argument("--seed", type=int, default=42)
    t.set_defaults(fn=cmd_select)
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
