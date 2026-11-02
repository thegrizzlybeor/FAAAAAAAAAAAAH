#!/usr/bin/env python3
"""
verify.py -- the "nothing was skipped" check. Runs after ALL fetch jobs.

Compares the plan (plan/plan.json: how many videos each job had to deliver) with the
reports the jobs wrote (reports/<job>.json: how many they really delivered).

  * a job with NO report (crashed, timed out, cancelled)  -> everything in it is missing
  * a job that delivered fewer than expected               -> missing
  * every URL that failed is listed with the reason

Outputs:
  - a table in the job summary
  - verify_out/missing_links.txt   (paste into the "links" input, with append=true, to retry)
  - exit code 1 if anything at all is missing, so the run is never green when it is incomplete
"""
import json
import os
import sys
from pathlib import Path

PLAN = Path(os.getenv("PLAN_FILE", "plan/plan.json"))
REPORTS = Path(os.getenv("REPORTS_DIR", "reports"))
OUT = Path(os.getenv("VERIFY_OUT", "verify_out"))


def load():
    plan = json.loads(PLAN.read_text())
    reports = {}
    for f in sorted(REPORTS.glob("*.json")) if REPORTS.exists() else []:
        try:
            r = json.loads(f.read_text())
            reports[str(r["job"])] = r
        except Exception as e:
            print(f"::warning::unreadable report {f}: {e}")
    return plan, reports


def check(plan, reports):
    """Returns (rows, problems, retry_urls). Pure function: easy to test."""
    rows, problems, retry = [], [], []
    per_link = {}
    for job in plan["jobs"]:
        r = reports.get(str(job["job"]))
        exp = job.get("expected", 0)
        d = per_link.setdefault(job["idx"], {"expected": 0, "got": 0, "jobs": 0, "bad_jobs": 0})
        d["expected"] += exp
        d["jobs"] += 1
        if r is None:
            d["bad_jobs"] += 1
            msg = (f"job {job['job']} (link {job['idx']}, range {job.get('range') or 'all'}): NO REPORT - "
                   f"the job crashed, timed out or was cancelled; its {exp or 'unknown number of'} file(s) are missing")
            problems.append(msg)
            retry.append((job["url"], f"whole link again (job {job['job']} has no report)"))
            continue
        d["got"] += r.get("downloaded", 0)
        short = exp and r.get("downloaded", 0) < exp
        if r.get("failed") or short or (not exp and r.get("downloaded", 0) == 0):
            d["bad_jobs"] += 1
        for f in r.get("failed", []):
            problems.append(f"job {job['job']}: {f['url']} - {f['why']}")
            retry.append((f["url"], f["why"]))
        if short and not r.get("failed"):
            problems.append(f"job {job['job']}: got {r.get('downloaded', 0)} of {exp}")
            retry.append((job["url"], f"job {job['job']} delivered {r.get('downloaded', 0)} of {exp}"))
    for pl in plan["links"]:
        d = per_link.get(pl["idx"], {"expected": 0, "got": 0, "jobs": 0, "bad_jobs": 0})
        exp = d["expected"]
        state = "OK" if (d["bad_jobs"] == 0 and (d["got"] >= exp)) else "INCOMPLETE"
        if pl.get("tool") == "chain" and d["got"] > 0 and d["bad_jobs"] == 0:
            state = "OK (count unknown)"
        if not pl.get("complete", True):
            state += " (listing may have been short)"
        rows.append((pl["idx"], pl["url"], exp if exp else "?", d["got"], state))
    return rows, problems, retry


def main():
    plan, reports = load()
    rows, problems, retry = check(plan, reports)

    total_exp = sum(r[2] for r in rows if isinstance(r[2], int))
    total_got = sum(r[3] for r in rows)
    lines = ["## Completeness check", "| # | Link | Expected | Downloaded | Status |", "|---|---|---|---|---|"]
    lines += [f"| {i} | {u} | {e} | {g} | {s} |" for i, u, e, g, s in rows]
    lines.append(f"\n**Total: {total_got} of {total_exp} expected files downloaded.**")
    if problems:
        lines.append(f"\n### {len(problems)} problem(s)")
        lines += [f"- {p}" for p in problems[:200]]
        if len(problems) > 200:
            lines.append(f"- ... and {len(problems) - 200} more (see missing_links.txt)")
    else:
        lines.append("\nEverything that was planned was downloaded.")

    unrec = 0
    for pl in plan["links"]:
        unrec += len(pl.get("unrecognized", []))
    if unrec:
        lines.append(f"\n_{unrec} other link(s) on the pages were not recognised as videos and were NOT "
                     f"tried; they are listed in the `plan` artifact (unrecognized_links.txt). "
                     f"Run again with scrape_all=true to try every link on the page._")
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")

    OUT.mkdir(exist_ok=True)
    seen, uniq = set(), []
    for u, why in retry:
        if u not in seen:
            seen.add(u)
            uniq.append((u, why))
    (OUT / "missing_links.txt").write_text("".join(f"{u}   # {why[:150]}\n" for u, why in uniq))
    if problems:
        print(f"::error::{len(problems)} problem(s): the run is INCOMPLETE. See the summary and the "
              f"'missing-links' artifact.")
        sys.exit(1)


if __name__ == "__main__":
    main()
