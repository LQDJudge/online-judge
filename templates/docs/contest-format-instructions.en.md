[TOC]

This page documents the contest formats supported by the LQDOJ. Each contest format implements different scoring rules and submission handling.

## 1. Default

The default contest format. This is the most commonly used format for most contests.

**Scoring:** The best submission for each problem is used for scoring. The total score is the sum of points from all problems.

**Time:** Cumulative time is calculated as the sum of submission times for all problems where points were earned.

**Tiebreaker:** Total cumulative time (lower is better).

**Configuration:** No configuration options.

## 2. ICPC

The ICPC (International Collegiate Programming Contest) format follows ACM-ICPC rules.

**Scoring:** Problems are scored as either solved (full points) or unsolved (0 points). The total score is the number of problems solved.

**Time:** Cumulative time is the sum of submission times for solved problems, plus penalty time.

**Penalty:** Each incorrect submission before the first correct submission adds a configurable penalty (default: 20 minutes).

**Tiebreaker:** Total cumulative time including penalties (lower is better).

**Configuration:**
- `penalty`: Number of penalty minutes for each incorrect submission (default: 20)

## 3. IOI

The IOI (International Olympiad in Informatics) format.

**Scoring:** The best submission for each problem is used. Supports partial scoring.

**Time:** Time penalties are optional and disabled by default.

**Tiebreaker:** None (ties are allowed).

**Configuration:**
- `cumtime`: Set to `true` to enable time penalties (default: `false`)

## 4. New IOI

An enhanced IOI format with support for hidden subtasks, introduced in IOI 2016.

**Scoring:** Similar to IOI format but with subtask-based scoring. Some subtasks can be hidden during the contest.

**Hidden Subtasks:** Supports hiding specific subtasks from participants during the contest. Results for hidden subtasks are only revealed after the contest ends.

**Time:** Time penalties are optional and disabled by default.

**Configuration:**
- `cumtime`: Set to `true` to enable time penalties (default: `false`)

## 5. AtCoder

The AtCoder contest format, following AtCoder's scoring rules.

**Scoring:** The best submission for each problem is used for scoring.

**Time:** Uses the maximum submission time among all solved problems (not cumulative).

**Penalty:** Each incorrect submission adds a configurable penalty (default: 5 minutes).

**Tiebreaker:** Total time including penalties (lower is better).

**Configuration:**
- `penalty`: Number of penalty minutes for each incorrect submission (default: 5)

## 6.ECOO

The ECOO (Educational Computing Organization of Ontario) format with bonus scoring.

**Scoring:** Uses the last submission for each problem. Includes bonus points for first-try solutions and time bonuses.

**Bonuses:**
- **First AC Bonus:** Extra points awarded for solving a problem on the first non-IE/CE submission
- **Time Bonus:** Extra points based on how early the solution was submitted

**Time:** Cumulative time is optional.

**Configuration:**
- `cumtime`: Set to `true` to use cumulative time for tiebreaking (default: `false`)
- `first_ac_bonus`: Points awarded for first-try AC solutions (default: 10)
- `time_bonus`: Minutes per bonus point for early submission (default: 5, set to 0 to disable)

## 7. Ultimate

A simplified format that only considers the last submission for each problem.

**Scoring:** Only the most recent submission for each problem is considered, regardless of its score.

**Time:** Time penalties are optional and disabled by default.

**Use Case:** Suitable for contests where participants should be encouraged to keep improving their solutions.

**Configuration:**
- `cumtime`: Set to `true` to enable time penalties (default: `false`)

---

## Choosing a Format

- **Default:** Best for most contests, educational purposes, and when you want the highest score to count
- **ICPC:** For ACM-ICPC style contests where problems are either solved or not
- **IOI:** For olympiad-style contests with partial scoring
- **New IOI:** For advanced olympiad contests with hidden subtasks
- **AtCoder:** For contests following AtCoder's penalty system
- **ECOO:** For contests with bonus scoring systems
- **Ultimate:** For contests where only the final submission matters

## Configuration Format

Contest format configurations are specified as JSON objects. For example:

```json
{
  "penalty": 20,
  "cumtime": true
}
```

If no configuration is needed, leave the field empty or use an empty object `{}`.