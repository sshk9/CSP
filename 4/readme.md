# RUN ORDER
## 1. Candidates: self-declared pronouns + country (same flags for every country)
python discover_pronouns.py --min-repos 5 --created-from 2012 --created-to 2020
python discover_pronouns.py --only "North Macedonia" --min-repos 5 --created-from 2012 --created-to 2020
####    check the table it prints: candidates per country and gender

## 2. Screening: until EVERY country x gender cell has 20 passes (slow, resumable)
python collect.py screen --target 20

## 3. Selection: equal feminine and masculine per country, with a small buffer
python collect.py select --quota 18

## 4. Metrics on the selected users (slow because of Pylint)
python metrics.py run --cell-target 15 --max-files 40

## 5. One row per user and period
python metrics.py aggregate

# Difference from 3
Finding Users: discover_pronouns.py: GitHub GraphQL search for users with a country in their location
Gender: Taken from explicit she/her or he/him pronouns, no hand-labelling
Strategy: Both genders are found by the same query and rules
Country: Required, taken from the location text with a check for ambiguous or rejected places, carried through every file
Sample: 15 per gender in each of 3 countries (45 + 45)
Screening Target: Per country × gender cell
Metrics: 	--cell-target per country × gender cell, extras act as buffer
