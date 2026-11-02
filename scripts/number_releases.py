#!/usr/bin/env python3
"""
number_releases.py -- gives every uploaded file and every release a plain number.

Runs once, AFTER all fetch jobs have uploaded their files.

  file names   ->  0.mp4, 1.mp4, 2.mp4, 3.webp ...   (number + original extension)
  release title -> "0", "1", "2" ...                  (the number of its file)
                   a release holding several files is titled "first-last", e.g. "3-7"

Numbering runs across ALL releases of this run, in link order (link 1 first, then
link 2, ...), and inside a release in file-name order.

With "append" the numbering carries on after the highest number that earlier
runs already used, so no number is ever used twice. The first ever file is 0.

Raw split files (name.mkv.part001, .part002 ...) count as ONE file: they all get
the same number (5.mkv.part001, 5.mkv.part002) so  cat 5.mkv.part* > 5.mkv  works.

Env: GITHUB_REPOSITORY, GH_TOKEN, RUN_NUMBER (or GITHUB_RUN_NUMBER), DRY_RUN=1 (only print the plan)
"""
import json
import os
import re
import subprocess
import sys
import time

REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN = os.environ.get("RUN_NUMBER") or os.environ.get("GITHUB_RUN_NUMBER") or "0"
DRY = os.environ.get("DRY_RUN", "") == "1"

THIS_TAG = re.compile(rf"^run-{re.escape(RUN)}-(\d+)(?:-p(\d+))?$")
ANY_RUN_TAG = re.compile(r"^run-\d+-")
SPLIT_PART = re.compile(r"^(.*)\.part\d{3}$")
NUMBERED = re.compile(r"^(\d+)(?:\.|$)")
NUM_TITLE = re.compile(r"^(\d+)(?:-(\d+))?$")
EXT = re.compile(r"(?:\.tar)?\.[A-Za-z0-9]{1,8}$")


# ----------------------------------------------------------------- GitHub I/O
def gh(*args):
    last = ""
    for attempt in range(1, 5):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        last = (p.stderr or p.stdout).strip()
        time.sleep(3 * attempt)
    raise RuntimeError(f"gh {' '.join(args)} failed: {last}")


def stream_json(text):
    dec, i, out = json.JSONDecoder(), 0, []
    text = text or ""
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = dec.raw_decode(text, i)
        out.append(obj)


def list_releases():
    out = gh("api", "--paginate", f"repos/{REPO}/releases?per_page=100", "--jq",
             ".[] | {id: .id, tag: .tag_name, title: .name, "
             "assets: [(.assets // [])[] | {id: .id, name: .name}]}")
    return stream_json(out)


def list_assets(release_id):
    out = gh("api", "--paginate", f"repos/{REPO}/releases/{release_id}/assets?per_page=100",
             "--jq", ".[] | {id: .id, name: .name}")
    return stream_json(out)


def rename_asset(asset_id, name):
    gh("api", "-X", "PATCH", f"repos/{REPO}/releases/assets/{asset_id}", "-f", f"name={name}")


def retitle_release(release_id, title):
    gh("api", "-X", "PATCH", f"repos/{REPO}/releases/{release_id}", "-f", f"name={title}")


# ------------------------------------------------------------------- planning
def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def ext_of(base):
    m = EXT.search(base)
    return m.group(0) if m else ""


def highest_used(releases):
    """Highest number already used by releases of OTHER runs (-1 if none)."""
    top = -1
    for r in releases:
        tag = r.get("tag") or ""
        if THIS_TAG.match(tag) or not ANY_RUN_TAG.match(tag):
            continue
        m = NUM_TITLE.match((r.get("title") or "").strip())
        if m:
            top = max(top, int(m.group(2) or m.group(1)))
        for a in r.get("assets") or []:
            m = NUMBERED.match(a["name"])
            if m:
                top = max(top, int(m.group(1)))
    return top


def plan_release(assets, first_number):
    """
    assets: [{"id", "name"}]. Returns (renames, next_number, title)
    renames: [(asset_id, old_name, new_name)] for every asset (changed or not).
    """
    groups = {}
    for a in assets:
        m = SPLIT_PART.match(a["name"])
        base = m.group(1) if m else a["name"]
        groups.setdefault(base, []).append(a)

    def order(base):
        m = NUMBERED.match(base)
        return (0, int(m.group(1)), []) if m else (1, 0, natural(base))

    n, renames, used = first_number, [], []
    for base in sorted(groups, key=order):
        ext = ext_of(base)
        for a in sorted(groups[base], key=lambda x: natural(x["name"])):
            part = SPLIT_PART.match(a["name"])
            suffix = a["name"][len(base):] if part else ""     # ".part001"
            renames.append((a["id"], a["name"], f"{n}{ext}{suffix}"))
        used.append(n)
        n += 1
    title = str(used[0]) if len(used) == 1 else f"{used[0]}-{used[-1]}"
    return renames, n, title


def apply_renames(renames):
    """Rename safely even when a new name is currently held by another asset."""
    todo = [r for r in renames if r[1] != r[2]]
    if not todo:
        return
    current = {r[1] for r in renames}
    clash = any(new in current and new != old for _, old, new in todo)
    if clash:  # park everything on a unique temporary name first
        for aid, _, _ in todo:
            rename_asset(aid, f"tmp-{aid}")
    for aid, _, new in todo:
        rename_asset(aid, new)


# ----------------------------------------------------------------------- main
def main():
    if not REPO:
        sys.exit("GITHUB_REPOSITORY is not set")
    releases = list_releases()
    mine = []
    for r in releases:
        m = THIS_TAG.match(r.get("tag") or "")
        if m:
            mine.append((int(m.group(1)), int(m.group(2) or 0), r))
    mine.sort(key=lambda t: (t[0], t[1]))

    start = highest_used(releases) + 1
    print(f"Numbering starts at {start}; {len(mine)} release(s) in run {RUN}")

    number, plans, rows = start, [], []
    for _, _, r in mine:
        assets = list_assets(r["id"])
        if not assets:
            if DRY:
                print(f"- {r['tag']}: no files, would be deleted")
            else:
                print(f"- {r['tag']}: no files, deleting the empty release")
                try:
                    gh("release", "delete", r["tag"], "--repo", REPO, "--cleanup-tag", "-y")
                except RuntimeError as e:
                    print(f"::warning::could not delete empty release {r['tag']}: {e}")
            continue
        renames, number, title = plan_release(assets, number)
        plans.append((r, renames, title))
        for _, old, new in renames:
            rows.append((r["tag"], old, new, title))

    for tag, old, new, title in rows:
        print(f"  [{tag}] {old}  ->  {new}   (release title: {title})")
    if DRY:
        print("DRY_RUN: nothing changed")
        return

    for r, renames, title in plans:
        apply_renames(renames)
        retitle_release(r["id"], title)

    # verify the result instead of trusting it
    errors = []
    for r, renames, title in plans:
        have = {a["name"] for a in list_assets(r["id"])}
        want = {new for _, _, new in renames}
        if have != want:
            errors.append(f"{r['tag']}: expected {sorted(want)} but found {sorted(have)}")
    if errors:
        print("::error::numbering verification failed")
        print("\n".join(errors))
        sys.exit(1)

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Numbering\n| Release | File |\n|---|---|\n")
            for _, _, new, title in rows:
                f.write(f"| {title} | {new} |\n")
    print(f"done: {len(rows)} file(s) numbered {start}..{number - 1}")


if __name__ == "__main__":
    main()
