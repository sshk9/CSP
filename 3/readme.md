Data-collection and measurement pipeline for a longitudinal, paired study of Python code on GitHub.
The same users are measured in a **pre-LLM** window and a **post-LLM** window, and grouped by
**inferred public gender presentation** (not confirmed gender identity).

This README covers the three scripts only: `collect.py`, `classify.py`, `metrics.py`.
It contains no results. The statistical analysis script (`analyze.py`) is not written yet.

---

## 1. Study windows

| Window | Dates (UTC) |
|---|---|
| Pre-LLM | 2021-01-01 to 2022-11-29 |
| Gap (excluded) | 2022-11-30 to 2023-12-31 |
| Post-LLM | 2024-01-01 to 2025-12-31 |

---

## 2. Setup

**Requirements:** Python 3.10+, `git` installed and on PATH, a GitHub account.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install requests pylint pycodestyle radon pandas
pip freeze > requirements.txt
```

**Environment variables** (set in every new terminal, or make them permanent):

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Personal access token (classic, **no scopes needed**). Gives 5,000 API requests/hour instead of 60. Can be changed freely between sessions. |
| `STUDY_SALT` | Any secret string. Hashes usernames into anonymous `uid`s. **Must stay identical for the whole project**, otherwise uids no longer match across files. Write it down. |

```powershell
$env:GITHUB_TOKEN = "ghp_..."
$env:STUDY_SALT   = "your-fixed-secret"
```

---

## 3. Pipeline at a glance

```
collect.py discover-repos  ->  candidates.csv          (who might qualify)
collect.py screen          ->  screen_log.csv, manifest.csv, repo_cache/   (who does qualify, which files)
classify.py sheet          ->  coding_A.csv            (hand-label gender presentation)
classify.py single | agree ->  consensus.csv
classify.py select         ->  selected_users.csv      (uids only)
metrics.py run             ->  files_metrics.csv       (one row per file)
metrics.py aggregate       ->  user_period.csv         (one row per user x period)
```

Order of work: **collect, then classify, then metrics.** Screening is automated, labelling is manual, so only users who pass screening are ever hand-labelled.

---

## 4. Step-by-step run

1. **Build the candidate pool**
   ```powershell
   python collect.py discover-repos --n 400
   python collect.py discover-repos --n 700 --fem-only --seed 7
   ```
   Both runs append to `candidates.csv` and skip logins already present. The second widens the pool using a first-name list (see Section 5).
2. **Screen a small batch first and check the pass rate**
   ```powershell
   python collect.py screen --target 10
   $log = Import-Csv screen_log.csv
   $log | Group-Object outcome | Select Name, Count | Sort Count -Descending
   ```
   If the pass rate is below about 5%, stop and fix the pool before spending hours cloning.
3. **Full screening** (slow, run in the background or overnight; resumable)
   ```powershell
   python collect.py screen --target 150
   ```
4. **Label users** (only those that passed)
   ```powershell
   python classify.py sheet
   ```
   Fill the `label` and `evidence` columns in `coding_A.csv`. Rerun `sheet` later to add newly passed users without losing existing labels.
5. **Build the consensus file and select users**
   ```powershell
   python classify.py single              # one coder
   # or, with two independent coders:
   python classify.py agree               # prints Cohen's kappa, writes consensus.csv
   python classify.py select --quota 40   # larger than needed, to allow for replacements
   ```
6. **Compute metrics** (slow because of Pylint; use `--max-files 10` for tests)
   ```powershell
   python metrics.py run --quota 30 --max-files 40
   python metrics.py aggregate
   ```

All steps are resumable. To restart a step from scratch, delete only that step's output files, **never `repo_cache/` or the labels**.

---

## 5. `collect.py`

### Commands

| Command | What it does |
|---|---|
| `discover` | Original user search (`type:user language:python`, spread over account-creation months). Low yield because user search cannot filter by activity. Kept for reference. |
| `discover-repos` | Current method. Searches repos created in `--y0..--y1` (default 2016-2021) and still pushed after 2025-01-01, keeps owners of type `User`. `--fem-only` keeps only profiles whose first name is in `FEM_NAMES`. |
| `screen` | For each candidate: checks eligibility via API, clones repos, applies thresholds, writes the file manifest. |

### What `screen` does, in order

1. Confirms the account is an individual user.
2. Lists owner repos and keeps: not a fork, not archived, non-empty, GitHub language = Python, size under 150 MB, pushed since 2021-01-01, name/description not matching tutorial/course/homework/template-style words.
3. Collects emails GitHub itself links to the login (API author-login match plus noreply addresses). **Display names are never used to attribute commits.**
4. **Cheap pre-screen before cloning:** if the API shows fewer than `MIN_COMMITS` commits by the user in either window, reject (`PRESCREEN_THIN_*`). Repos with no commits in either window are not cloned.
5. Bare-clones each remaining repo (max `MAX_REPOS`) and reads the git history of the default branch.
6. **Single-contributor rule:** the user must own at least 90% of the repo's Python-touching commits, otherwise the repo is skipped.
7. Per window, counts the user's commits (ignoring bulk commits), active ISO weeks, and records the **latest version** of every eligible `.py` file by commit hash.
8. The user passes only if **both** windows meet all thresholds.

### Thresholds (top of file, applied to each window)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_COMMITS` | 5 | User's Python commits after bulk filter |
| `MIN_WEEKS` | 2 | Distinct active ISO weeks |
| `MIN_FILES` | 3 | Distinct eligible Python files |
| `BULK_THRESHOLD` | 30 | Commits touching more `.py` files than this are ignored (imports, mass reformatting) |
| `MIN_AUTHOR_SHARE` | 0.90 | Single-contributor rule |
| `MAX_REPOS` | 8 | Repos inspected per user |
| size cap | 150,000 KB | Skips very large repos (speed; large repos are rarely personal projects) |
| clone timeout | 300 s | A slow clone is skipped, not waited on |

These numbers are pragmatic, not taken from any research. No standard definition of an "active developer" exists for sample inclusion (existing definitions are for effort estimation or ecosystem reports and differ by orders of magnitude). Keep them identical for all users and report them in the methods.

### Excluded paths and files

Vendored or third-party folders, `venv`, `build`, `dist`, `migrations`, docs, examples, tutorials, `*_pb2.py`, `setup.py`, `conftest.py`, `__init__.py`. Files whose first lines say "auto-generated" or "do not edit" are dropped later in `metrics.py`.

### Outputs (all PRIVATE: contain real logins)

| File | Content |
|---|---|
| `candidates.csv` | Profile fields (name, bio, avatar URL, location) for labelling |
| `screen_log.csv` | One row per screened user: outcome and counts |
| `manifest.csv` | `uid, period, repo, path, commit` for every file to analyse |
| `repo_cache/` | Bare clones (large; reusable; do not delete) |

**Outcome codes:** `OK`, `THIN_PRE`, `THIN_POST`, `PRESCREEN_THIN_PRE/POST`, `NO_REPOS`, `NOT_USER`, `ERROR:<type>`.

### Improvements needed (`collect.py`)

- **Sampling is not random.** The frame (repos created 2016-2021, pushed after 2025) favours persistent developers, and `--fem-only` is name-targeted. Document as selection bias; compare against a random subsample if possible.
- **`FEM_NAMES` is short and culturally narrow.** Extend it, or replace name targeting with a better frame.
- **Commit dates are author dates**, which a user can rewrite. Acceptable, but state it.
- **Email matching can miss aliases** (commits made under an email GitHub did not link to the account), which causes false rejections. Only commits seen in API samples (max 100 per window per repo) contribute emails.
- **The pre-screen counts commits in all languages**, so it is only an upper bound. It rejects safely but cannot confirm Python activity.
- **`language == Python` drops notebook-dominant and mixed-language repos.** Consider counting `.py` files directly.
- **Only the default branch is read.** Work on other branches is missed.
- **No detection of copied code, bot commits, or co-authored commits** beyond path/name regexes and the 90% rule.
- **A user's "latest version" of a file may contain other people's edits** (reduced by the 90% rule, not eliminated).
- **Experience covariates are not collected** (account age, total prior commits). They are needed for the main confound in the analysis.
- **Dates and thresholds are hard-coded.** Move them into command-line options or a config file.
- **No tests.** Add a small test repo to check window logic, bulk filter and author share.
- **Old `discover` command is dead weight.** Remove once `discover-repos` is final.

---

## 6. `classify.py`

Labels **inferred public gender presentation** from information the profile itself shows. It does not determine gender identity.

### Commands

| Command | What it does |
|---|---|
| `sheet` | Builds `coding_A.csv` (and `coding_B.csv`) for users with outcome `OK`, in shuffled order. Never overwrites existing labels; only adds new users. |
| `single` | Single-coder mode: turns `coding_A.csv` directly into `consensus.csv` (no kappa). |
| `agree` | Two-coder mode: reports raw agreement, Cohen's kappa (4 labels and masculine-vs-feminine only), a confusion matrix, and writes `consensus.csv` with blanks where coders disagree. |
| `select` | Randomly picks up to `--quota` masculine and feminine users from consensus. Writes `selected_users.csv` (uids only). Reports any shortfall. |

### Labels and codebook

| Label | Use when |
|---|---|
| `masculine` / `feminine` | Two markers agree, **or** one explicit marker (e.g. pronouns in bio) with nothing contradicting it |
| `ambiguous` | Markers conflict, unisex or unknown name, or only one weak marker |
| `anonymous` | No usable name, photo or bio (logo, cartoon, initials) |

Markers allowed: name, username, avatar, bio, linked public profiles. Location may help read a name but is never evidence alone. Do not use code or repos to decide, and do not search outside the profile.

**Evidence column:** short tags joined by `+` so they can be counted later: `name`, `avatar`, `pronouns`, `bio`, `username`, `social`, `none`, `conflict`, `weak`. Example: `name+avatar`.

Only `masculine` and `feminine` users enter the study. `ambiguous` and `anonymous` are excluded and the counts should be reported.

### Practical notes

- Label in a spreadsheet but save as **CSV UTF-8** so accents survive.
- When unsure, choose `ambiguous`. A single coder with no check should be conservative.
- With two coders, label independently and do not discuss cases until both sheets are finished, or kappa is meaningless.
- Selection is random within each group by design, so it cannot depend on any code metric.

### Improvements needed (`classify.py`)

- **Single coder means no reliability estimate.** Have a second coder label a random subset (about 20-30 users) blind, and report kappa on it. Kappa on very small or one-sided samples is unstable.
- **Binary masculine/feminine only.** Non-binary presentation is not represented and falls into `ambiguous`. State this limitation.
- **Coding is manual and subjective** (avatars especially). Add a worked-example sheet to the codebook and calibrate on a few cases.
- **Evidence tags are not validated** by the script. Add a check that tags come from the allowed list.
- **The feminine-presenting group is small** (a minority of GitHub users and of screened users). Continue screening, or plan a balanced matched comparison instead of equal quotas.
- **Selection ignores experience.** Consider stratifying or matching on account age and activity.
- **Labels and sheets contain names and avatars.** Keep them private; do not commit them.

---

## 7. `metrics.py`

Computes metrics for each retained file of each selected user, then aggregates to user x period (files from one developer are not independent, so analysis should use user-level values or mixed models).

### Commands

| Command | What it does |
|---|---|
| `run` | For each selected user, reads the file versions listed in `manifest.csv` from the bare clones, computes metrics, writes `files_metrics.csv`. Takes the first `--quota` users per group that succeed; failures go to `metrics_skipped.csv`. Resumable. |
| `aggregate` | Averages file metrics within user x period (NaN-skipping), adds `n_files`, `total_sloc`, `n_repos`, writes `user_period.csv`. |

### File selection per user and period

- Up to `--max-files` files, chosen **randomly** (never by metric value), seeded for reproducibility.
- File must parse with `ast.parse` (this removes Python 2 and broken files), be under 300 KB, have at least 10 source lines, and not carry an "auto-generated" marker in its first 5 lines.
- A user needs at least 3 usable files in each period, otherwise they are skipped.

### Metric groups

| Group | Metrics | Source |
|---|---|---|
| **Module structure** | `loc, sloc, blank, comment_lines, docstring_lines, n_functions, n_methods, n_classes, avg_function_len, comment_ratio, docstring_ratio` | Brooke; `radon.raw` and `ast` |
| **Nesting depth** | `max_nesting` (control-flow depth: if/for/while/try/with/match) | Own addition, `ast` |
| **Cyclomatic complexity** | `cc_mean, cc_max, cc_total` (functions and methods only, no double-counting of classes) | Xu et al.; `radon.complexity` |
| **Halstead** | `h_vocabulary, h_length, h_volume, h_difficulty, h_effort, h_time, h_bugs` | Xu et al.; `radon.metrics` |
| **Maintainability** | `mi_std = 171 - 5.2 ln(V) - 0.23 CC - 16.2 ln(SLOC)` (raw, not rescaled to 0-100, so it can be negative); `mi_custom = mi_std + 50 sin(sqrt(2.4 CR))` | Xu et al.; computed in the script |
| **Naming** | Share of identifiers per category for **variables, functions, files**: `single, upper, snake, pascal, camel, lower`; plus `*_digit_end` (ends with a digit) and `*_len_mean` | Xu et al.; `ast` |
| **Style (lint)** | Pylint convention, warning, refactor, error(+fatal) counts and pycodestyle error count, each **per 1,000 SLOC** | Brooke; Pylint, pycodestyle |

Notes on definitions:

- **Naming precedence** (each name lands in one category): single letter, then UPPER, then snake_case, then PascalCase, then camelCase, then lowercase. Names that fit none are `other` (not output as a column). Leading/trailing underscores are stripped; dunders, `self`, `cls` and `_` are skipped. `digit_end` is an overlapping flag.
- **Variable names** come from assignments and function arguments; **function names** from `def`/`async def`; **file name** is the module's file stem.
- **Naming values are proportions per file**, so large files do not dominate.
- **Lint counts are normalised per 1,000 SLOC** so longer files do not automatically get more messages.
- **Pylint runs with an empty config** and ignores `import-error` and `no-name-in-module` (dependencies are not installed). Pylint's overall score is not used. pycodestyle uses its defaults (79-character lines). A fresh checker is used per file so counts do not accumulate.
- `avg_function_len` is NaN for files with no functions. NaN means "not applicable" and is skipped in aggregation.
- Halstead values and MI are very size-sensitive: aggregate at user level and control for file size (or log-transform) in the analysis.

### Other columns recorded

- `formatter_cfg`: 1 if the repo (at that commit) has a config mentioning Black, Ruff, YAPF or autopep8. Auto-formatting erases style differences, so use it as a covariate or sensitivity filter.
- `repo_hash`, `file_hash`, `uid`, `presentation`, `period`: anonymised identifiers.
- `tool_versions.txt`: Python, Pylint, pycodestyle and Radon versions. Report these in the methods.

### Outputs

`files_metrics.csv` (file level), `user_period.csv` (user x period), `metrics_skipped.csv` (users dropped and why), `tool_versions.txt`.

### Improvements needed (`metrics.py`)

- **Verify against reference tools.** Spot-check one file with `radon cc -s` and `pycodestyle --count`, and confirm the custom MI definition (comment ratio as comments/LOC, constant 2.4, fraction not percentage) against Xu et al.
- **Controls are not computed.** Developer experience (account age, prior commits), repo type/size and Python version are the main confounds and still need to be added.
- **Formatter use is only flagged.** Decide whether to exclude formatted repos or model them as a covariate, and run both versions as a sensitivity analysis.
- **Not implemented from the plan:** LLM-signal proxy (e.g. mentions of Copilot/ChatGPT in commits or READMEs), code similarity (cosine/Jaccard) for the convergence question (RQ4), and the blinded human readability rating with Krippendorff's alpha.
- **Python version drift.** `ast.parse` depends on the interpreter running the script, so code using newer syntax may be dropped on an older Python. Use one fixed Python version.
- **Generated-code detection is weak** (first 5 lines only). Add checks for known generators and unusually uniform files.
- **Lint detail is coarse.** Only four Pylint categories are kept. Consider selected individual messages (e.g. naming, missing docstrings) because they map more directly onto style.
- **Lint ignores each project's own config**, which may be intended (comparability) but should be stated.
- **Sampling per user is capped and random.** Check that results are stable across seeds and `--max-files`.
- **Runtime.** Pylint dominates; consider caching per file hash and running in parallel.
- **No automated tests.** Add a few fixture files with known metric values.
- **`analyze.py` is missing.** Planned: user-level paired comparisons, mixed model `metric ~ period * presentation + covariates + (1|user)`, effect sizes with confidence intervals, Holm or Benjamini-Hochberg correction across metrics, and a power check for the interaction.

---

## 8. Privacy and ethics

- **Private (never share or commit):** `candidates.csv`, `screen_log.csv`, `manifest.csv`, `coding_*.csv`, `consensus.csv`, `repo_cache/`, and anything containing logins, names, avatars or repo names.
- **Shareable:** files keyed only by `uid`, hashed repo/file ids: `selected_users.csv`, `files_metrics.csv`, `user_period.csv`.
- Do not publish `STUDY_SALT`. Anyone with it and a username list can re-identify uids.
- Only public data is used. Check GitHub's Terms of Service and your institution's ethics requirements. Do not name individual users in the paper.
- Gender labels are **inferred public presentation**, not gender identity. State this wherever results are reported.
---
## 9. Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Very slow screening | Token not set (check `echo $env:GITHUB_TOKEN`); large repos (size cap and clone timeout address this) |
| Pass rate under 5% | Candidate pool has dormant users; use `discover-repos`, not user search |
| uids do not match older files | `STUDY_SALT` changed; restore it or regenerate log and manifest |
| `classify.py` rejects labels | Use the exact words `masculine`, `feminine`, `ambiguous`, `anonymous` |
| `metrics.py` skips a user | See `metrics_skipped.csv` (`FEW_FILES_*` means under 3 parseable files of at least 10 SLOC) |
| Leftover half-clone after Ctrl+C | Delete that one folder in `repo_cache/` and rerun |
