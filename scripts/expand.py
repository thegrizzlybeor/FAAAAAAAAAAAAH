#!/usr/bin/env python3
"""
expand.py -- the "planner".

Turns the raw link list into many small, balanced jobs so that a 500 GB / 900-video
page never lands on one runner, and so that NOTHING is silently left out.

For every link it counts the videos/files with up to three methods and keeps the one
that finds the MOST:

  gallery-dl   gallery-dl -J        (sites gallery-dl supports; gives sizes)
  yt-dlp       yt-dlp --flat-playlist -J   (playlists, channels, tube sites)
  scrape       reads the page's HTML and collects every link that looks like a video
               (direct .mp4/.m3u8 files, or pages a known extractor supports).
               SCRAPE_ALL=1 -> EVERY link on the page is treated as a video page.

The files are cut into chunks (CHUNK_GB each, at most CHUNK_MAX_ITEMS files). Every chunk
becomes one parallel job. The full plan (incl. every item URL) is saved to plan/plan.json so
the fetch jobs and verify.py know exactly what is expected.

Nothing is dropped: if the jobs don't fit into GitHub's 256-job matrix limit the chunks are
made bigger; if they STILL don't fit the planner stops with an error instead of cutting.

Env: INPUT_LINKS, CHUNK_GB (8), CHUNK_MAX_ITEMS (40), UNKNOWN_ITEM_MB (600, per unknown-size
     VIDEO), SCRAPE_ALL (0/1), COOKIES, CREATE_RELEASES (1/0), GITHUB_RUN_NUMBER,
     GITHUB_REPOSITORY, GITHUB_SHA
"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

GB = 1024 ** 3
MB = 1024 ** 2
CHUNK_BYTES = float(os.getenv("CHUNK_GB", "8")) * GB
MAX_ITEMS = int(os.getenv("CHUNK_MAX_ITEMS", "40"))
UNKNOWN_BYTES = {"VID": int(float(os.getenv("UNKNOWN_ITEM_MB", "600")) * MB),
                 "IMG": 5 * MB, "AUD": 20 * MB, "FILE": 150 * MB}
SCRAPE_ALL = os.getenv("SCRAPE_ALL", "0") == "1"
MAX_JOBS = 256          # GitHub matrix limit
MAX_ASSETS = 500        # a release holds at most 1000 files; keep slack for split parts
LIST_TRIES = 3
LIST_RETRY_SLEEP = float(os.getenv("LIST_RETRY_SLEEP", "3"))
RUN = os.getenv("GITHUB_RUN_NUMBER", "0")
PLAN_DIR = Path(os.getenv("PLAN_DIR", "plan"))

IMG_X = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".avif", ".heic"}
VID_X = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts", ".wmv", ".3gp",
         ".mpg", ".mpeg", ".ogv", ".mts", ".m2ts", ".vob", ".m3u8"}
AUD_X = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
ASSET_X = {".css", ".js", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot", ".map", ".xml", ".rss"}
TITLE_KEYS = ("album_name", "album_title", "title", "album", "gallery", "folder", "name", "subcategory_name")
DIRECT_EXT = (IMG_X | VID_X | AUD_X) - {".m3u8"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ helpers
def read_links():
    raw = os.environ.get("INPUT_LINKS", "").strip()
    if not raw:
        lines = []
        if Path("links.txt").exists():
            for line in open("links.txt", encoding="utf-8"):
                line = line.split(" #")[0].strip()
                if line and not line.startswith("#"):
                    lines.append(line)
        raw = "\n".join(lines)
    links = []
    for tok in re.split(r"\s+", raw):
        for u in re.split(r",(?=https?://)", tok):
            u = u.strip().strip(",")
            if u and u not in links:
                links.append(u)
    return links


def ext_of_url(u):
    return Path(urlparse(str(u)).path).suffix.lower()


def kind_of_ext(ext):
    ext = ext.lower()
    return "IMG" if ext in IMG_X else "VID" if ext in VID_X else "AUD" if ext in AUD_X else "FILE"


def target_of(url):
    return "direct://" + url if ext_of_url(url) in DIRECT_EXT else url


def cookie_args():
    return ["--cookies", "cookies.txt"] if Path("cookies.txt").exists() else []


def fallback_title(url):
    return Path(urlparse(url).path).name or urlparse(url).netloc


def clean_title(t):
    return re.sub(r"\s+", " ", t or "").strip()


# ----------------------------------------------------------- listing methods
def list_gallery(url):
    cmd = ["gallery-dl", "-J", "--retries", "4"] + cookie_args() + [target_of(url)]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800)
        data = json.loads(p.stdout)
        complete = p.returncode == 0
    except Exception:
        return None
    items, title = [], ""
    for m in data:
        if not (isinstance(m, list) and m):
            continue
        meta = next((x for x in m[1:] if isinstance(x, dict)), {})
        if not title:
            for key in TITLE_KEYS:
                v = meta.get(key)
                if isinstance(v, str) and v.strip():
                    title = v.strip()
                    break
        if m[0] == 3:  # 3 = file URL message
            size = 0
            for key in ("size", "filesize", "file_size", "bytes"):
                v = meta.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    size = int(v)
                    break
            ext = "." + str(meta.get("extension") or "").lower().lstrip(".")
            if ext == ".":
                ext = ext_of_url(m[1])
            items.append({"u": str(m[1]), "s": size, "k": kind_of_ext(ext)})
    if not items:
        return None
    return {"items": items, "title": clean_title(title) or fallback_title(url), "complete": complete}


def _flatten(node, out):
    ents = node.get("entries")
    if ents is None:
        out.append(node)
        return
    for e in ents:
        if isinstance(e, dict):
            _flatten(e, out)


def list_ytdlp(url):
    cmd = ["yt-dlp", "--flat-playlist", "-J", "--no-warnings", "--ignore-errors"] + cookie_args() + [url]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=1800)
        data = json.loads(p.stdout)
        complete = p.returncode == 0
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    title = clean_title(data.get("title") or "") or fallback_title(url)
    if "entries" not in data:  # a single video page
        return {"items": [{"u": url, "s": int(data.get("filesize") or data.get("filesize_approx") or 0), "k": "VID"}],
                "title": title, "complete": complete}
    leaves = []
    _flatten(data, leaves)
    items, unresolved = [], 0
    for e in leaves:
        u = e.get("url") or e.get("webpage_url")
        if u and not str(u).startswith("http"):
            u = e.get("webpage_url")
        if not (u and str(u).startswith("http")):
            unresolved += 1
        items.append({"u": str(u) if u and str(u).startswith("http") else None,
                      "s": int(e.get("filesize") or e.get("filesize_approx") or 0), "k": "VID"})
    if not items:
        return None
    if unresolved:  # can't address entries by URL -> the job will use yt-dlp --playlist-items ranges
        for it in items:
            it["u"] = None
    return {"items": items, "title": title, "complete": complete}


_YT_CLASSES = None


def _ytdlp_classes():
    global _YT_CLASSES
    if _YT_CLASSES is None:
        try:
            from yt_dlp.extractor import gen_extractor_classes
            _YT_CLASSES = [ie for ie in gen_extractor_classes() if not ie.IE_NAME.startswith("generic")]
        except Exception:
            _YT_CLASSES = []
    return _YT_CLASSES


def recognized(u):
    """True if the link is a direct media file or a page a specific extractor supports."""
    if ext_of_url(u) in DIRECT_EXT | {".m3u8"}:
        return True
    try:
        import gallery_dl.extractor as gx
        if gx.find(u) is not None:
            return True
    except Exception:
        pass
    for ie in _ytdlp_classes():
        try:
            if ie.suitable(u):
                return True
        except Exception:
            continue
    return False


LINK_ATTR = re.compile(r"""(?:href|src|data-src|data-url|data-href|data-video|data-mp4|content)\s*=\s*["']([^"'#][^"']*)["']""", re.I)
RAW_MEDIA = re.compile(r"""https?:[^"'\s<>\\]+?\.(?:m3u8|mp4|webm|mov|mkv|m4v)(?:\?[^"'\s<>\\]*)?""", re.I)


def list_scrape(url):
    """Returns (listing or None, unrecognized_links)."""
    if ext_of_url(url) in DIRECT_EXT:
        return None, []
    try:
        import requests
        jar = None
        if Path("cookies.txt").exists():
            try:
                from http.cookiejar import MozillaCookieJar
                jar = MozillaCookieJar("cookies.txt")
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                jar = None
        r = requests.get(url, headers={"User-Agent": UA, "Referer": url}, cookies=jar, timeout=60)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html").lower():
            return None, []
        html = r.text.replace("\\/", "/").replace("&amp;", "&")
    except Exception:
        return None, []
    found = []
    for raw in LINK_ATTR.findall(html) + RAW_MEDIA.findall(html):
        low = raw.strip().lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        u = urldefrag(urljoin(r.url, raw.strip()))[0]
        if urlparse(u).scheme not in ("http", "https"):
            continue
        if ext_of_url(u) in ASSET_X or u.rstrip("/") == url.rstrip("/") or u in found:
            continue
        found.append(u)
    items, unrec = [], []
    for u in found:
        if SCRAPE_ALL or recognized(u):
            items.append({"u": u, "s": 0, "k": kind_of_ext(ext_of_url(u)) if ext_of_url(u) else "VID"})
        else:
            unrec.append(u)
    if not items:
        return None, unrec
    return {"items": items, "title": fallback_title(url), "complete": True}, unrec


def best_listing(fn, url):
    best = None
    for attempt in range(LIST_TRIES):
        r = fn(url)
        if r and (best is None or len(r["items"]) > len(best["items"])):
            best = r
        if r and r["complete"]:
            break
        time.sleep(LIST_RETRY_SLEEP)
    return best


def list_link(url):
    """Run all three methods, keep the one that finds the most items."""
    gd = best_listing(list_gallery, url)
    yt = best_listing(list_ytdlp, url)
    sc, unrec = list_scrape(url)
    cands = [("gallery-dl", gd), ("yt-dlp", yt), ("scrape", sc)]
    counts = {n: (len(r["items"]) if r else 0) for n, r in cands}
    pick, listing = max(((n, r) for n, r in cands if r), key=lambda t: len(t[1]["items"]), default=(None, None))
    return {"pick": pick, "listing": listing, "counts": counts, "unrecognized": unrec}


def tool_of(pick, listing):
    if pick == "gallery-dl":
        return "gallery"
    if all(it["u"] for it in listing["items"]):
        return "items"
    return "ytdlp-range"


# ------------------------------------------------------------------ planning
def label_of(items):
    """IMG / VID / AUD / FILE: what most of the album is (by bytes, else by count)."""
    weight = {}
    for it in items:
        weight[it["k"]] = weight.get(it["k"], 0) + (it["s"] or UNKNOWN_BYTES[it["k"]])
    return max(weight, key=weight.get)


def chunk(sizes, cap, max_items):
    out, start, acc, n = [], 1, 0, 0
    for i, s in enumerate(sizes, 1):
        if n and (acc + s > cap or n >= max_items):
            out.append((start, i - 1))
            start, acc, n = i, 0, 0
        acc += s
        n += 1
    if n:
        out.append((start, len(sizes)))
    return out


def eff_sizes(items):
    return [it["s"] or UNKNOWN_BYTES[it["k"]] for it in items]


def plan(links, listed):
    cap, max_items = CHUNK_BYTES, MAX_ITEMS
    while True:
        per_link = {}
        for idx, url in enumerate(links, 1):
            info = listed[url]["listing"]
            per_link[idx] = chunk(eff_sizes(info["items"]), cap, max_items) if info else None
        total = sum(len(c) if c else 1 for c in per_link.values())
        if total <= MAX_JOBS:
            return per_link, cap
        if cap > 40 * GB:
            print(f"::error::{total} jobs are needed but GitHub allows {MAX_JOBS} per run, and chunks "
                  f"cannot get any bigger. Nothing was started, so nothing is skipped silently. "
                  f"Split the links into several runs (use append=true to keep the earlier ones).")
            sys.exit(1)
        cap, max_items = cap * 1.5, max_items * 2  # too many jobs -> make chunks bigger


def precreate_releases(include, tags):
    """Create every release up front, in a fixed order, so GitHub lists them in order.

    GitHub sorts releases by creation time (newest first). If each fetch job created its
    own release, the order would depend on which job finished first -> scrambled numbers.
    So the releases are created here, highest number first, lowest last: the lowest
    number ends up on top of the list. Releases that stay empty are deleted later by
    number_releases.py.
    """
    if os.getenv("CREATE_RELEASES", "1") != "1":
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    if not repo:
        return
    order = []
    for e in include:
        if e["tag"] not in order:
            order.append(e["tag"])
    for tag in reversed(order):
        cmd = ["gh", "release", "create", tag, "--repo", repo,
               "--title", tags.get(tag, tag), "--notes", ""]
        if sha:
            cmd += ["--target", sha]
        ok = False
        for _ in range(3):
            if subprocess.run(cmd, capture_output=True, text=True).returncode == 0:
                ok = True
                break
            time.sleep(3)
        if not ok:
            print(f"::warning::could not pre-create release {tag}; its fetch job will create it")
        time.sleep(1.2)  # release timestamps have 1 second resolution


def main():
    if os.getenv("COOKIES"):
        Path("cookies.txt").write_text(os.environ["COOKIES"])
    links = read_links()
    if not links:
        sys.exit("::error::no links given (input empty and links.txt missing/empty)")
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(list_link, links))
    listed = dict(zip(links, results))
    per_link, cap = plan(links, listed)
    if cap != CHUNK_BYTES:
        print(f"::notice::many files -> chunk size raised to {cap / GB:.0f} GB to stay under {MAX_JOBS} jobs")

    include, rows, tags, plan_links = [], [], {}, []
    for idx, url in enumerate(links, 1):
        res = listed[url]
        chunks, info = per_link[idx], res["listing"]
        if not chunks:  # nothing could be listed -> one job that runs the whole fallback chain
            name = f"FILE | {fallback_title(url)}"
            tag = f"run-{RUN}-{idx}"
            include.append({"job": f"{idx}", "idx": idx, "part": "", "range": "", "url": url, "tag": tag,
                            "tool": "chain", "expected": 0})
            tags[tag] = name[:100]
            rows.append((idx, name, url, "?", "?", 1, "could NOT be listed - runs as one job, count unknown"))
            plan_links.append({"idx": idx, "url": url, "tool": "chain", "count": 0, "unrecognized": res["unrecognized"]})
            print(f"::warning::link {idx}: could not be listed by any method; it will run as one job and the "
                  f"number of videos cannot be checked: {url}")
            continue

        tool = tool_of(res["pick"], info)
        items = info["items"]
        title = f"{label_of(items)} | {info['title']}"
        per_chunk_tag = len(items) > MAX_ASSETS
        for k, (a, b) in enumerate(chunks, 1):
            part = f"p{k:02d}" if len(chunks) > 1 else ""
            rng = f"{a}-{b}" if (len(chunks) > 1 or tool != "gallery") else ""
            tag = f"run-{RUN}-{idx}" + (f"-{part}" if per_chunk_tag and part else "")
            include.append({"job": f"{idx}-{part}" if part else f"{idx}", "idx": idx, "part": part,
                            "range": rng, "url": url, "tag": tag, "tool": tool, "expected": b - a + 1})
            if tag not in tags:
                tags[tag] = (f"{title} (part {k}/{len(chunks)})" if per_chunk_tag else title)[:100]
        known = sum(it["s"] for it in items)
        unknown = sum(1 for it in items if not it["s"])
        est = f"{known / GB:.1f} GB" + (f" + {unknown} unknown" if unknown else "")
        note = f"via {res['pick']}; found " + ", ".join(f"{n}={c}" for n, c in res["counts"].items())
        if not info["complete"]:
            note += "; WARNING listing may be incomplete"
            print(f"::warning::link {idx}: listing finished with errors, the count may be too low: {url}")
        if res["unrecognized"] and not SCRAPE_ALL:
            note += f"; {len(res['unrecognized'])} other links on the page were not recognised as videos (see plan artifact)"
        rows.append((idx, title, url, len(items), est, len(chunks), note))
        plan_links.append({"idx": idx, "url": url, "tool": tool, "count": len(items), "complete": info["complete"],
                           "counts": res["counts"], "unrecognized": res["unrecognized"],
                           "items": [it["u"] for it in items] if tool == "items" else []})

    # Pass the title along to the fetch jobs (they create the release only if it is missing).
    for e in include:
        e["title"] = tags.get(e["tag"], e["tag"])

    # Save the plan BEFORE anything else, so verify.py always knows what was expected.
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    (PLAN_DIR / "plan.json").write_text(json.dumps({"run": RUN, "jobs": include, "links": plan_links}, indent=1))
    unrec_lines = []
    for pl in plan_links:
        for u in pl["unrecognized"]:
            unrec_lines.append(f"{pl['idx']}\t{u}")
    (PLAN_DIR / "unrecognized_links.txt").write_text("\n".join(unrec_lines) + ("\n" if unrec_lines else ""))

    precreate_releases(include, tags)

    summary = ["## Plan", "| # | Release | Link | Files | Size | Jobs | Notes |", "|---|---|---|---|---|---|---|"]
    summary += [f"| {i} | {t.replace('|', chr(92) + '|')} | {u} | {n} | {s} | {j} | {note} |"
                for i, t, u, n, s, j, note in rows]
    total_expected = sum(e["expected"] for e in include)
    summary.append(f"\n**{total_expected} files expected in {len(include)} job(s).** "
                   f"verify.py will compare this number with what was really downloaded.")
    text = "\n".join(summary)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"matrix={json.dumps({'include': include})}\n")
            f.write(f"count={len(include)}\n")
    print(f"{len(include)} job(s) queued")


if __name__ == "__main__":
    main()
