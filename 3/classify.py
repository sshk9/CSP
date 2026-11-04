"""classify.py - label public gender presentation, check agreement, select 30/30.
Usage:
  python classify.py sheet            # makes coding_A.csv and coding_B.csv
  python classify.py agree            # kappa + consensus.csv
  python classify.py select --quota 30
Labels: masculine | feminine | ambiguous | anonymous
These are inferred PUBLIC PRESENTATION labels, not gender identity.
"""

import argparse, csv, random
from collections import Counter
from pathlib import Path

# CONFIG AND CODING SHEET ---------------------------------------------------------------
LABELS = ("masculine", "feminine", "ambiguous", "anonymous")
CODEBOOK = """
CODEBOOK (both coders read before starting; do not discuss individual cases)
- masculine / feminine: use ONLY what the profile itself shows: name, username,
  avatar, bio (pronouns, self-description), linked public profiles.
  Label only if at least two markers agree, or one explicit marker (e.g. pronouns
  in bio) with nothing contradicting it.
- ambiguous: markers conflict, unisex/unknown names, or one weak marker only.
- anonymous: no usable name, photo, or bio (logo, cartoon, initials only).
- Location may help read a name but is never evidence by itself.
- Do not use the code or repos to decide. Do not search outside the profile.
- Fill `evidence` with the markers used (e.g. "name+avatar", "pronouns").
"""
SHEET_FIELDS = ["uid", "profile_url", "login", "name", "bio", "location", "blog", "avatar_url", "label", "evidence"]

def read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

def cmd_sheet(args):
    log = [r for r in read_csv(args.log) if r["outcome"] == "OK"]
    cands = {r["login"]: r for r in read_csv(args.candidates)}
    base = []
    for r in log:
        c = cands.get(r["login"])
        if not c:
            continue
        base.append({"uid": r["uid"], "profile_url": f"https://github.com/{r['login']}",
                     "login": r["login"], "name": c["name"], "bio": c["bio"],
                     "location": c["location"], "blog": c["blog"],
                     "avatar_url": c["avatar_url"], "label": "", "evidence": ""})
    for coder in ("A", "B"):
        out = Path(f"coding_{coder}.csv")
        existing = {}
        if out.exists():
            existing = {r["uid"]: r for r in read_csv(out)}
        rows = list(existing.values())
        new = [r for r in base if r["uid"] not in existing]
        random.Random(f"{args.seed}{coder}").shuffle(new)
        write_csv(out, rows + new, SHEET_FIELDS)
        print(f"{out}: {len(rows)} existing + {len(new)} new users")
    print(CODEBOOK)
    print("PRIVATE files: contain names and avatars. Share each sheet only with its coder.")

# AGREEMENT & CONSENSUS
def cohen_kappa(a, b):
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0

def load_labels(path):
    rows = read_csv(path)
    bad = [r["uid"] for r in rows if r["label"].strip().lower() not in LABELS + ("",)]
    if bad:
        raise SystemExit(f"{path}: invalid labels for {bad[:5]}. Allowed: {LABELS}")
    return {r["uid"]: r["label"].strip().lower() for r in rows if r["label"].strip()}

def cmd_agree(args):
    a, b = load_labels("coding_A.csv"), load_labels("coding_B.csv")
    both = sorted(set(a) & set(b))
    if len(both) < 10:
        raise SystemExit(f"Only {len(both)} users labelled by both coders. Finish labelling first.")
    la, lb = [a[u] for u in both], [b[u] for u in both]
    print(f"Users labelled by both: {len(both)}")
    print(f"Raw agreement: {sum(x == y for x, y in zip(la, lb)) / len(both):.1%}")
    print(f"Cohen's kappa (4 labels): {cohen_kappa(la, lb):.3f}")
    fm = [(x, y) for x, y in zip(la, lb) if x in ("masculine", "feminine") and y in ("masculine", "feminine")]
    if len(fm) > 1:
        print(f"Kappa, masculine vs feminine only: {cohen_kappa(*zip(*fm)):.3f} (n={len(fm)})")
    print("Confusion (A rows, B cols):")
    cnt = Counter(zip(la, lb))
    print("            " + " ".join(f"{l[:9]:>9}" for l in LABELS))
    for x in LABELS:
        print(f"{x:>11} " + " ".join(f"{cnt[(x, y)]:>9}" for y in LABELS))
    rows = [{"uid": u, "label_A": a[u], "label_B": b[u],
             "consensus": a[u] if a[u] == b[u] else ""} for u in both]
    write_csv("consensus.csv", rows, ["uid", "label_A", "label_B", "consensus"])
    n_dis = sum(not r["consensus"] for r in rows)
    print(f"\nWrote consensus.csv. {n_dis} disagreements: coders discuss these, then fill "
          f"`consensus` (or put ambiguous). Disagreements left blank are excluded.")

# SELECTION AND CLI
def cmd_select(args):
    rows = [r for r in read_csv("consensus.csv")
            if r["consensus"].strip().lower() in ("masculine", "feminine")]
    by = {"masculine": [], "feminine": []}
    for r in rows:
        by[r["consensus"].strip().lower()].append(r["uid"])
    rnd = random.Random(args.seed)
    out = []
    for g, uids in by.items():
        print(f"{g}: {len(uids)} eligible (need {args.quota})")
        rnd.shuffle(uids)        # random pick: never select on metric values
        out += [{"uid": u, "presentation": g} for u in uids[:args.quota]]
        if len(uids) < args.quota:
            print(f"  SHORT by {args.quota - len(uids)}: screen more candidates")
    write_csv("selected_users.csv", out, ["uid", "presentation"])
    print(f"Wrote selected_users.csv ({len(out)} users). Contains uids only: safe for metrics.py.")

def cmd_single(args):
    a = load_labels("coding_A.csv")
    rows = [{"uid": u, "label_A": l, "label_B": "", "consensus": l} for u, l in a.items()]
    write_csv("consensus.csv", rows, ["uid", "label_A", "label_B", "consensus"])
    print(f"Wrote consensus.csv from coder A only ({len(rows)} users). No kappa: single coder.")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("--log", default="screen_log.csv")
    s.add_argument("--candidates", default="candidates.csv")
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(fn=cmd_sheet)
    a = sub.add_parser("agree")
    a.set_defaults(fn=cmd_agree)
    t = sub.add_parser("select")
    t.add_argument("--quota", type=int, default=30)
    t.add_argument("--seed", type=int, default=42)
    t.set_defaults(fn=cmd_select)
    sg = sub.add_parser("single")
    sg.set_defaults(fn=cmd_single)
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
