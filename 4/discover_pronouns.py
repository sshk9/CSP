#!/usr/bin/env python3
"""discover_pronouns.py - find GitHub users with explicit she/her or he/him pronouns
in Brazil, India and North Macedonia. Same query and same rules for both groups.
Resumable (appends, skips logins already saved).
Usage:
  python discover_pronouns.py --test      # one country/term/year, checks the pronouns field works
  python discover_pronouns.py             # full run
Env: GITHUB_TOKEN
Output: pronoun_users.csv (PRIVATE: contains logins)
"""
import argparse
import csv
import os
import re
import sys
import time
from datetime import date, timedelta
import requests

# CONFIG ------------------------------------------------------------------------------------------
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set GITHUB_TOKEN first.")
URL = "https://api.github.com/graphql"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# "terms" are searched in the free-text profile location AND used to confirm the country.
# "reject" words disqualify a location (other countries sharing a place name).
COUNTRIES = {
    "Brazil": {
        "min_repos": 5,
        "terms": ["Brazil", "Brasil", "São Paulo", "Sao Paulo", "Rio de Janeiro", "Brasília",
                  "Brasilia", "Belo Horizonte", "Curitiba", "Porto Alegre", "Recife",
                  "Fortaleza", "Campinas", "Florianópolis", "Bahia"],
        "reject": ["portugal", "angola", "mozambique", "el salvador"],
    },
    "India": {
        "min_repos": 5,
        "terms": ["India", "Bangalore", "Bengaluru", "Mumbai", "New Delhi", "Delhi",
                  "Hyderabad", "Chennai", "Pune", "Kolkata", "Noida", "Gurgaon", "Gurugram",
                  "Ahmedabad", "Jaipur"],
        "reject": ["pakistan", "bangladesh", "nepal", "sri lanka"],
    },
    "North Macedonia": {
        "min_repos": 5,
        "terms": ["North Macedonia", "Macedonia", "Skopje", "Bitola", "Ohrid", "Tetovo",
                  "Kumanovo", "Prilep", "Štip", "Strumica", "Veles"],
        "reject": ["greece", "greek", "thessaloniki", "athens", "bulgaria", "ohio"],
    },
}
LANGUAGE = "python"
MAX_PAGES = 20

FIELDS = ["login", "profile_url", "group", "pronouns", "country", "location", "account_created", "last_push"]

QUERY = """
query($q: String!, $after: String) {
  search(type: USER, query: $q, first: 50, after: $after) {
    userCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on User {
      login url pronouns location createdAt
      repositories(first: 1, ownerAffiliations: OWNER,
                   orderBy: {field: PUSHED_AT, direction: DESC}) {
        nodes { pushedAt }
      }
    } }
  }
}
"""

# CLASSIFYING PRONOUNS & COUNTRY-------------------------------------------------------------------
def classify(pronouns):
    """Explicit she/her -> feminine, explicit he/him -> masculine.
    they/them only, custom, empty, or BOTH explicit pairs -> excluded."""
    p = (pronouns or "").lower().replace(" ", "")
    s, h = "she/her" in p, "he/him" in p
    if s and not h:
        return "feminine"
    if h and not s:
        return "masculine"
    return None

def _has(text, word):
    return re.search(r"(?<!\w)" + re.escape(word.lower()) + r"(?!\w)", text) is not None

def country_of(location):
    """Country from the profile's free-text location, or None if it is missing,
    ambiguous (matches two countries) or contains a rejecting word. Heuristic."""
    text = (location or "").lower()
    if not text.strip():
        return None
    hits = []
    for country, cfg in COUNTRIES.items():
        if any(_has(text, r) for r in cfg["reject"]):
            continue
        if any(_has(text, t) for t in cfg["terms"]):
            hits.append(country)
    return hits[0] if len(hits) == 1 else None

# GRAPHQL CALL, AUTOMATIC SPLITTING, HANDLING USERS------------------------------------------------
def gql(variables):
    for attempt in range(6):
        try:
            r = requests.post(URL, headers=HEADERS, timeout=60,
                              json={"query": QUERY, "variables": variables})
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (403, 429, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", 30 * (attempt + 1)))
            print(f"  busy / rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        errs = data.get("errors") or []
        if any(e.get("type") == "RATE_LIMITED" for e in errs):
            time.sleep(60)
            continue
        if errs:
            raise RuntimeError(errs)     # e.g. "Field 'pronouns' doesn't exist"
        return data["data"]["search"]
    raise RuntimeError("Too many retries")

def handle_user(u, seen, writer, fh, stats, args):
    """Apply the SAME rules to everyone: pronouns, country, creation year, last push."""
    if not u or u["login"].lower() in seen:
        return
    seen.add(u["login"].lower())
    group = classify(u.get("pronouns"))
    if not group:
        stats["no_explicit_pronouns"] += 1
        return
    country = country_of(u.get("location"))
    if not country:
        stats["country_unclear"] += 1
        return
    created = int(u["createdAt"][:4])
    if not (args.created_from <= created <= args.created_to):
        stats["created_out_of_range"] += 1
        return
    nodes = (u.get("repositories") or {}).get("nodes") or []
    last_push = nodes[0].get("pushedAt") if nodes else None
    if not last_push or int(last_push[:4]) < args.last_push_year:
        stats["not_recently_pushed"] += 1
        return
    row = {"login": u["login"], "profile_url": u["url"], "group": group,
           "pronouns": u["pronouns"], "country": country,
           "location": u["location"] or "", "account_created": u["createdAt"],
           "last_push": last_push}
    writer.writerow(row)
    fh.flush()
    stats["kept"] += 1
    print(f"  + {group[:4]} {country}: {u['login']}", file=sys.stderr)

def build_query(term, min_repos, a, b):
    q = f'location:"{term}" repos:>={min_repos} created:{a.isoformat()}..{b.isoformat()}'
    return q + (f" language:{LANGUAGE}" if LANGUAGE else "")

def search_range(term, min_repos, a, b, seen, writer, fh, stats, args):
    """GitHub returns at most 1,000 results per search. If a date range matches more,
    split it in half and search each half, down to single days."""
    q = build_query(term, min_repos, a, b)
    s = gql({"q": q, "after": None})
    time.sleep(2)
    if s["userCount"] > 1000 and a < b:
        mid = a + (b - a) // 2
        search_range(term, min_repos, a, mid, seen, writer, fh, stats, args)
        search_range(term, min_repos, mid + timedelta(days=1), b, seen, writer, fh, stats, args)
        return
    if s["userCount"] > 1000:
        stats["truncated_days"] += 1
        print(f"  WARNING: {s['userCount']} users on {a} for '{term}'; only 1,000 reachable",
              file=sys.stderr)
    page = 0
    while True:
        for u in s["nodes"]:
            handle_user(u, seen, writer, fh, stats, args)
        page += 1
        if not s["pageInfo"]["hasNextPage"] or page >= MAX_PAGES:
            break
        s = gql({"q": q, "after": s["pageInfo"]["endCursor"]})
        time.sleep(2)

# MAIN---------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="pronoun_users.csv")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--created-from", type=int, default=2014)
    ap.add_argument("--created-to", type=int, default=2018)
    ap.add_argument("--last-push-year", type=int, default=2026, help="latest push must be in this year or later (relax to 2025 if short)")
    ap.add_argument("--min-repos", type=int, default=None, help="one value for ALL countries (overrides the per-country defaults)")
    ap.add_argument("--only", default=None, help='search just one country, e.g. "North Macedonia"')
    args = ap.parse_args()

    seen = set()
    new = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    if not new:
        with open(args.out, encoding="utf-8") as f:
            seen = {r["login"].lower() for r in csv.DictReader(f)}
    fh = open(args.out, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=FIELDS)
    if new:
        writer.writeheader()

    progress_path = args.out + ".progress"
    done_terms = set()
    if os.path.exists(progress_path):
        with open(progress_path, encoding="utf-8") as pf:
            done_terms = {line.strip() for line in pf if line.strip()}

    stats = {"kept": 0, "no_explicit_pronouns": 0, "country_unclear": 0,
             "created_out_of_range": 0, "not_recently_pushed": 0, "truncated_days": 0}
    for country, cfg in COUNTRIES.items():
        if args.test and country != "Brazil":
            continue
        if args.only and country != args.only:                       # <-- ADD THESE 2 LINES
            continue
        min_repos = args.min_repos if args.min_repos is not None else cfg["min_repos"]
        terms = cfg["terms"][:1] if args.test else cfg["terms"]
        print(f"Country: {country} (min repos {min_repos})", file=sys.stderr)
        for term in terms:
            key = (f"{country}|{term}|{min_repos}|"
                   f"{args.created_from}-{args.created_to}|{args.last_push_year}")
            if not args.test and key in done_terms:
                print(f"Skipping finished term: {term}", file=sys.stderr)
                continue
            print(f"Term: {term}", file=sys.stderr)
            a = date(2016, 1, 1) if args.test else date(args.created_from, 1, 1)
            b = date(2016, 12, 31) if args.test else date(args.created_to, 12, 31)
            search_range(term, min_repos, a, b, seen, writer, fh, stats, args)
            if not args.test:                     # mark done only after the term finishes
                with open(progress_path, "a", encoding="utf-8") as pf:
                    pf.write(key + "\n")
    fh.close()

    with open(args.out, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print("\nCandidates per country and group (the numbers that decide the sample):")
    for c in COUNTRIES:
        f_n = sum(r["country"] == c and r["group"] == "feminine" for r in rows)
        m_n = sum(r["country"] == c and r["group"] == "masculine" for r in rows)
        print(f"  {c:16s} feminine {f_n:5d}   masculine {m_n:5d}")
    print("Dropped this run:", {k: v for k, v in stats.items() if k != "kept"})
    print(f"Wrote {len(rows)} users -> {args.out} (PRIVATE)")

if __name__ == "__main__":
    main()
