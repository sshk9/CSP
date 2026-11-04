Pipeline for a paired, longitudinal study: the same GitHub users are measured in a **pre-LLM** window (2021-01-01 to 2022-11-29) and a **post-LLM** window (2024-01-01 to 2025-12-31), grouped by **inferred public gender presentation** (not gender identity). Changes are described as occurring *during* a period, not as caused by LLMs.

Scripts: `collect.py`, `classify.py`, `metrics.py`. `analyze.py` is not written yet. No results are included here.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1           # Mac/Linux: source .venv/bin/activate
pip install requests pylint pycodestyle radon pandas
pip freeze > requirements.txt
$env:GITHUB_TOKEN = "ghp_..."        # classic token, no scopes needed
$env:STUDY_SALT   = "fixed-secret"   # never change it: it makes the anonymous uids
```

`git` must be installed. Set both variables in every new terminal.

## Run order

```powershell
python collect.py discover-repos --n 400
python collect.py discover-repos --n 700 --fem-only --seed 7
python collect.py screen --target 10          # check pass rate first
python collect.py screen --target 150         # slow, resumable
python classify.py sheet                      # then label coding_A.csv by hand
python classify.py single                     # or: agree (two coders, gives kappa)
python classify.py select --quota 40
python metrics.py run --quota 30 --max-files 40
python metrics.py aggregate
```

All steps are resumable. When restarting a step, delete only its outputs, never `repo_cache/` or the labels.

## collect.py

- **discover-repos:** finds owners of repos created 2016-2021 and still pushed after 2025. `--fem-only` keeps profiles whose first name is in a name list (widens the pool only; humans still label presentation).
- **screen:** keeps individual accounts with non-fork, non-archived, Python repos under 150 MB. Commits are attributed only through emails GitHub links to the login. It rejects cheaply via the API before cloning, requires the user to own at least 90% of a repo's Python commits, and keeps the latest version of each eligible file per window.
- **Thresholds (per window, pragmatic, not from the literature):** 5 commits, 2 active weeks, 3 files; commits touching more than 30 `.py` files are ignored. Vendored, docs, examples, tutorial, migration, `setup.py` and `__init__.py` paths are excluded.
- **Outputs (private):** `candidates.csv`, `screen_log.csv`, `manifest.csv`, `repo_cache/`.

## classify.py

- **sheet** makes the coding sheet for users who passed. **single** or **agree** builds `consensus.csv` (agree reports Cohen's kappa). **select** randomly picks equal quotas per group and writes `selected_users.csv` (uids only).
- **Labels:** `masculine`, `feminine`, `ambiguous`, `anonymous`. Label masculine/feminine only if two markers (name, avatar, bio, pronouns, linked profile) agree, or one explicit marker has no contradiction. Location is never evidence. When unsure, use `ambiguous`. Ambiguous and anonymous users are excluded.
- **Evidence column:** tags joined by `+`, e.g. `name+avatar`.

## metrics.py

Random sample of up to `--max-files` files per user and period (files must parse, have at least 10 source lines, and not be marked auto-generated; at least 3 per period).

| Group | Metrics |
|---|---|
| Module structure | lines, source lines, blanks, comments, docstrings, functions, methods, classes, average function length |
| Nesting | maximum control-flow depth |
| Complexity | cyclomatic (mean, max, total); Halstead (vocabulary, length, volume, difficulty, effort, time, bugs) |
| Maintainability | standard and custom MI (Xu et al. formulas, raw scale) |
| Naming | share of single-letter, UPPER, snake, Pascal, camel, lowercase names for variables, functions and files; ends-with-digit share; mean name length |
| Style | Pylint (convention, warning, refactor, error) and pycodestyle counts per 1,000 source lines |

`formatter_cfg` flags repos with Black/Ruff/YAPF/autopep8 config. `aggregate` averages files to one row per user and period (`user_period.csv`). Tool versions are saved in `tool_versions.txt`.

## Privacy

Keep private: candidates, log, manifest, coding sheets, consensus, `repo_cache/`, and the salt. Only uid-keyed files (`selected_users.csv`, `files_metrics.csv`, `user_period.csv`) are shareable. Use only public data and check GitHub's terms and your ethics requirements.

## Improvements needed

**collect.py**
- Sampling is non-random (favours persistent developers; name-targeted for feminine-presenting users) and the name list is short and narrow.
- Email matching can miss aliases; only the default branch is read; author dates can be rewritten.
- The API pre-screen counts all languages; the `language == Python` filter drops notebook-heavy repos.
- No detection of copied code, bots or co-authors beyond the 90% rule.
- No experience covariates (account age, prior commits); dates and thresholds are hard-coded; no tests.

**classify.py**
- One coder means no reliability estimate: have a second coder label a random 20-30 users blind and report kappa.
- Binary labels only; coding is subjective; evidence tags are not validated.
- The feminine-presenting group is small; consider a matched comparison and matching on experience.

**metrics.py**
- Verify against `radon cc` and `pycodestyle --count`, and check the custom MI definition against Xu et al.
- Confounds not yet computed: experience, project type and size, Python version; formatter use is flagged but not handled.
- Not implemented: LLM-signal proxy, code similarity (RQ4), blinded readability ratings with Krippendorff's alpha.
- Generated-code detection is weak; `ast.parse` depends on the Python version used; Pylint is slow; no tests.
- `analyze.py` is missing: paired user-level comparisons, mixed model `metric ~ period * presentation + covariates + (1|user)`, effect sizes with confidence intervals, multiple-comparison correction, power check.
