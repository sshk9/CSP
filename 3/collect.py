"""collect.py - discover GitHub users, screen eligibility, write a file manifest.
Usage:
  python collect.py discover --n 600
  python collect.py screen --target 200
Env: GITHUB_TOKEN, STUDY_SALT.
"""
# CONFIG --------------------------------------------------------------------------------
import argparse
import calendar
import csv
import hashlib
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

UTC = timezone.utc
WINDOWS = {  # gap = 30 Nov 2022 to 31 Dec 2023
    "pre":  (datetime(2021, 1, 1, tzinfo=UTC), datetime(2022, 11, 29, 23, 59, 59, tzinfo=UTC)),
    "post": (datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)),
}
MIN_COMMITS = 5
MIN_WEEKS = 2
MIN_FILES = 2
BULK_THRESHOLD = 30
MIN_AUTHOR_SHARE = 0.90 
MAX_REPOS = 8
DELAY = 0.5

EXCLUDE_PATH = re.compile(
    r"(^|/)(vendor|vendored|third_party|site-packages|node_modules|venv|\.venv|env|"
    r"build|dist|migrations|docs?|examples?|tutorials?)(/|$)|_pb2(_grpc)?\.py$|"
    r"(^|/)(setup|conftest)\.py$|(^|/)__init__\.py$", re.I)
EXCLUDE_REPO = re.compile(
    r"tutorial|course|homework|assignment|bootcamp|leetcode|exercise|cheatsheet|"
    r"template|boilerplate|learn", re.I)

# UTILS ---------------------------------------------------------------------------------
class GitError(RuntimeError):
    pass

def log(msg):
    print(msg, file=sys.stderr, flush=True)

def uid_for(login):
    salt=os.environ.get("STUDY_SALT")
    if not salt:
        raise SystemExit("Set STUDY_SALT (any secret string) before running.")
    return hashlib.sha256((salt + login.lower()).encode()).hexdigest()[:10]

def run(cmd, timeout=900):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
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

# DISCOVER ------------------------------------------------------------------------------
CAND_FIELDS = ["login", "github_id", "name", "bio", "avatar_url", "location",
               "blog", "created_at", "public_repos"]

def month_slices(y0, y1):
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            yield f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]}"

def cmd_discover(args):
    """Resumable. --query-extra adds first-name terms to the SEARCH ONLY (widens the pool);
    presentation is still coded by humans from the profile. Document as targeted sampling."""
    rnd = random.Random(args.seed)
    out = Path(args.out)
    seen = set()
    if out.exists():
        seen = {r["login"] for r in csv.DictReader(open(out, encoding="utf-8"))}
    new_file = not out.exists()
    fh = open(out, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(fh, fieldnames=CAND_FIELDS)
    if new_file:
        w.writeheader()
    total = len(seen)
    jobs = [(t, a, b) for t in (args.query_extra or [None])
            for a, b in month_slices(args.y0, args.y1)]
    rnd.shuffle(jobs)
    for term, a, b in jobs:
        if total >= args.n:
            break
        q = f"type:user language:python repos:{args.min_repos}..200 created:{a}..{b}"
        if term:
            q += f" fullname:{term}"
        for page in range(1, args.pages + 1):
            res = api_get("/search/users", {"q": q, "per_page": 30, "page": page})
            time.sleep(2)  # search API: 30 req/min
            items = (res or {}).get("items", [])
            for item in items:
                login = item["login"]
                if login in seen:
                    continue
                seen.add(login)
                p = api_get(f"/users/{login}")
                if not p or p.get("type") != "User":
                    continue
                w.writerow({"login": login, "github_id": p["id"], "name": p.get("name") or "",
                            "bio": (p.get("bio") or "").replace("\n", " "),
                            "avatar_url": p.get("avatar_url", ""), "location": p.get("location") or "",
                            "blog": p.get("blog") or "", "created_at": p.get("created_at"),
                            "public_repos": p.get("public_repos")})
                fh.flush()
                total += 1
            if len(items) < 30:
                break
        log(f"{total} candidates ({term or 'no name filter'}, {a})")
    log(f"Wrote {out}. PRIVATE file: contains names and avatars.")

# REPOSITORY ANALYSIS -------------------------------------------------------------------
def clone(url, dest):
    if dest.exists():
        git(dest, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/heads/*")
    else:
        r = run(["git", "clone", "--bare", "--quiet", url, str(dest)], timeout=300)
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
    keep =     keep = [r for r in repos
            if not r["fork"] and not r["archived"] and not r.get("disabled")
            and 0 < r["size"] < 150_000 and r.get("language") == "Python"
            and (r.get("pushed_at") or "") >= "2021-01-01"
            and not EXCLUDE_REPO.search(r["name"] + " " + (r.get("description") or ""))]
    return keep[:MAX_REPOS]

def verified_emails(profile, repos):
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
    """Per window: user's commit count, active weeks, and latest version
    (commit hash) of each eligible file. None if repo fails the single-author rule."""
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

# USER SCREENING AND CLI ----------------------------------------------------------------
LOG_FIELDS = ["uid", "login", "outcome", "repos_used",
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
    for w in WINDOWS:
        if sum(c[w] for c in counts.values()) < MIN_COMMITS:
            return f"PRESCREEN_THIN_{w.upper()}", {}, []
    repos = [r for r in repos if any(counts[r["full_name"]].values())]
    win = {w: {"commits": 0, "weeks": set(), "rows": []} for w in WINDOWS}
    used = 0
    for r in repos:
        dest = cache / f"{r['full_name'].replace('/', '__')}.git"
        try:
            log(f"    cloning {r['name']}")
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
    cands = list(csv.DictReader(open(args.candidates, encoding="utf-8")))
    random.Random(args.seed).shuffle(cands)
    log_path, man_path = Path(args.log), Path(args.manifest)
    done = set()
    n_ok = 0
    if log_path.exists():
        rows = list(csv.DictReader(open(log_path, encoding="utf-8")))
        done = {r["login"] for r in rows}
        n_ok = sum(r["outcome"] == "OK" for r in rows)
    lf = open(log_path, "a", newline="", encoding="utf-8")
    lw = csv.DictWriter(lf, fieldnames=LOG_FIELDS)
    if not done:
        lw.writeheader()
    mf = open(man_path, "a", newline="", encoding="utf-8")
    mw = csv.DictWriter(mf, fieldnames=MANIFEST_FIELDS)
    if mf.tell() == 0:
        mw.writeheader()
    for c in cands:
        if n_ok >= args.target:
            break
        login = c["login"]
        if login in done:
            continue
        log(f"[{n_ok}/{args.target}] {login}")
        try:
            outcome, stats, rows = screen_user(login, cache)
        except Exception as exc:  # keep long runs alive
            outcome, stats, rows = f"ERROR:{type(exc).__name__}", {}, []
        log(f"   -> {outcome}")
        lw.writerow({"uid": uid_for(login), "login": login, "outcome": outcome, **stats})
        mw.writerows(rows)
        lf.flush(); mf.flush()
        n_ok += outcome == "OK"
    log(f"Done. {n_ok} users passed screening. Manifest/log are PRIVATE (contain logins/repos).")

FEM_NAMES = {"maria", "anna", "sarah", "emily", "priya", "olga", "yuki", "mei", "elena",
             "laura", "julia", "sofia", "emma", "sara", "anastasia", "natalia", "ana",
             "kate", "katie", "jessica", "jennifer", "lisa", "amy", "hannah", "alice",
             "sophie", "ines", "chloe", "fatima", "aisha", "xin", "yan"}

def cmd_discover_repos(args):
    """Owners of repos created in [y0,y1] and still pushed since 2025 => plausibly
    active in both windows. --fem-only widens the pool by first name only;
    presentation is still coded by humans from the profile. Resumable."""
    rnd = random.Random(args.seed)
    out = Path(args.out)
    seen = set()
    if out.exists():
        seen = {r["login"] for r in csv.DictReader(open(out, encoding="utf-8"))}
    new_file = not out.exists()
    fh = open(out, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(fh, fieldnames=CAND_FIELDS)
    if new_file:
        w.writeheader()
    total = len(seen)
    slices = list(month_slices(args.y0, args.y1))
    rnd.shuffle(slices)
    for a, b in slices:
        if total >= args.n:
            break
        q = (f"language:python fork:false archived:false size:>100 "
             f"created:{a}..{b} pushed:>2025-01-01")
        for page in range(1, args.pages + 1):
            res = api_get("/search/repositories",
                          {"q": q, "sort": "updated", "per_page": 100, "page": page})
            time.sleep(2)
            items = (res or {}).get("items", [])
            for item in items:
                owner = item.get("owner") or {}
                login = owner.get("login")
                if owner.get("type") != "User" or not login or login in seen:
                    continue
                seen.add(login)
                try:
                    p = api_get(f"/users/{login}")
                except GitError:
                    log(f"    skip {login}: API error")
                    continue
                if not p or p.get("type") != "User":
                    continue
                first = (p.get("name") or "").strip().split(" ")[0].lower()
                if args.fem_only and first not in FEM_NAMES:
                    continue
                w.writerow({"login": login, "github_id": p["id"], "name": p.get("name") or "",
                            "bio": (p.get("bio") or "").replace("\n", " "),
                            "avatar_url": p.get("avatar_url", ""), "location": p.get("location") or "",
                            "blog": p.get("blog") or "", "created_at": p.get("created_at"),
                            "public_repos": p.get("public_repos")})
                fh.flush()
                total += 1
            if len(items) < 100:
                break
        log(f"{total} candidates ({a})")
    log(f"Wrote {out}. PRIVATE file.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--n", type=int, default=600)
    d.add_argument("--out", default="candidates.csv")
    d.add_argument("--seed", type=int, default=42)
    d.add_argument("--query-extra", nargs="*", help="first names, e.g. Maria Priya Sarah Anna")
    d.add_argument("--y0", type=int, default=2012, help="earliest account-creation year")
    d.add_argument("--y1", type=int, default=2019, help="latest account-creation year")
    d.add_argument("--min-repos", type=int, default=10)
    d.add_argument("--pages", type=int, default=3)
    d.set_defaults(fn=cmd_discover)
    r = sub.add_parser("discover-repos")
    r.add_argument("--n", type=int, default=1000)
    r.add_argument("--out", default="candidates.csv")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--y0", type=int, default=2016)
    r.add_argument("--y1", type=int, default=2021)
    r.add_argument("--pages", type=int, default=3)
    r.add_argument("--fem-only", action="store_true")
    r.set_defaults(fn=cmd_discover_repos)
    s = sub.add_parser("screen")
    s.add_argument("--candidates", default="candidates.csv")
    s.add_argument("--target", type=int, default=200, help="stop after this many users pass")
    s.add_argument("--cache", default="repo_cache")
    s.add_argument("--log", default="screen_log.csv")
    s.add_argument("--manifest", default="manifest.csv")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(fn=cmd_screen)
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
