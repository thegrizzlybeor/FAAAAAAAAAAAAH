#!/usr/bin/env python3
"""
scripts/downloaders.py - try several download tools in a smart order until one works.

Usage from fetch.py:

    from downloaders import download
    result = download(url, "out/job1", range_=RANGE)   # RANGE like "1-50" or ""
    print(result["winner"], result["files"])
    # result["attempts"] = [{"tool":..., "rc":..., "files":..., "seconds":..., "error":...}, ...]

Test from a shell:  python scripts/downloaders.py "https://..." ./test_out

Env vars used (all optional):
    COOKIES        Netscape cookies.txt contents (your existing secret)
    MAX_HEIGHT     max video height for yt-dlp (default 720)
    DL_TIMEOUT_MIN minutes allowed per tool (default 45)
    DL_TOOLS       comma list to force order/limit, e.g. "ytdlp,gallery_dl"
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif", ".bmp", ".tiff",
    ".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".3gp",
    ".wmv", ".mpg", ".mpeg", ".ogv", ".mts", ".m2ts", ".vob",
    ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac",
}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts", ".3gp",
             ".wmv", ".mpg", ".mpeg", ".ogv", ".mts", ".m2ts", ".vob"}
JUNK_SUFFIX = (".part", ".ytdl", ".json", ".txt", ".tmp", ".temp", ".aria2", ".info.json")
MIN_BYTES = 2048  # smaller than this = error page / placeholder, not media

# hosts where yt-dlp is almost always the better first choice
VIDEO_HOSTS = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|tiktok\.com|twitch\.tv|"
    r"facebook\.com/.*/videos|fb\.watch|instagram\.com/(reel|tv)|x\.com/.*/status|"
    r"twitter\.com/.*/status|bilibili\.com|rumble\.com|streamable\.com|"
    r"pornhub\.com|xvideos\.com|xhamster\.com|spankbang\.com|reddit\.com/r/.*/comments)",
    re.I,
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def log(msg):
    print(f"[dl] {msg}", flush=True)


def media_files(d: Path):
    out = []
    for p in Path(d).rglob("*"):
        if (p.is_file() and p.suffix.lower() in MEDIA_EXT
                and not p.name.lower().endswith(JUNK_SUFFIX)
                and p.stat().st_size >= MIN_BYTES):
            out.append(p)
    return out


def has_video(files):
    return any(Path(f).suffix.lower() in VIDEO_EXT for f in files)


def total_bytes(files):
    return sum(f.stat().st_size for f in files)


def run(cmd, timeout):
    """Run a command, stream a short tail of output, return (rc, tail)."""
    log("$ " + " ".join(str(c) for c in cmd)[:300])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        tail = (p.stdout + "\n" + p.stderr).strip().splitlines()[-8:]
        for line in tail:
            print("    " + line[:300], flush=True)
        return p.returncode, "\n".join(tail)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, f"{cmd[0]} not installed"


class Ctx:
    def __init__(self, url, range_, workdir):
        self.url = url
        self.range = (range_ or "").strip()
        self.timeout = int(float(os.environ.get("DL_TIMEOUT_MIN", "45")) * 60)
        self.max_height = os.environ.get("MAX_HEIGHT", "720")
        self.cookies = None
        raw = os.environ.get("COOKIES", "").strip()
        if raw:
            self.cookies = Path(workdir) / "cookies.txt"
            self.cookies.write_text(raw + "\n")


# ---------------------------------------------------------------- strategies
def s_gallery_dl(c: Ctx, dest: Path):
    cmd = ["gallery-dl", "-d", str(dest), "--no-mtime", "-o", "retries=5"]
    if c.cookies:
        cmd += ["--cookies", str(c.cookies)]
    if c.range:
        cmd += ["--range", c.range]
    return run(cmd + [c.url], c.timeout)


def _ytdlp_base(c: Ctx, dest: Path):
    h = c.max_height
    cmd = [
        "yt-dlp", "--no-warnings", "--ignore-errors", "--no-overwrites",
        "--retries", "5", "--fragment-retries", "10", "--concurrent-fragments", "4",
        "-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(dest / "%(title).120B [%(id)s].%(ext)s"),
        "--windows-filenames",
    ]
    if c.cookies:
        cmd += ["--cookies", str(c.cookies)]
    if c.range:
        cmd += ["--playlist-items", c.range]
    return cmd


def s_ytdlp(c: Ctx, dest: Path):
    return run(_ytdlp_base(c, dest) + [c.url], c.timeout)


def s_ytdlp_impersonate(c: Ctx, dest: Path):
    # browser TLS fingerprint (needs curl_cffi) - beats many Cloudflare-style blocks
    return run(_ytdlp_base(c, dest) + ["--impersonate", "chrome", c.url], c.timeout)


def s_ytdlp_generic(c: Ctx, dest: Path):
    # forces the generic extractor: scans the page for embedded players / m3u8 / mp4
    cmd = _ytdlp_base(c, dest) + [
        "--use-extractors", "generic", "--extractor-args", "generic:impersonate",
        "--impersonate", "chrome", c.url,
    ]
    return run(cmd, c.timeout)


def s_direct(c: Ctx, dest: Path):
    """The URL itself is a file. Stream it with requests (aria2c if installed)."""
    import requests
    headers = {"User-Agent": UA}
    try:
        r = requests.get(c.url, headers=headers, stream=True, timeout=30, allow_redirects=True)
    except Exception as e:
        return 1, str(e)
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    ext = Path(urlparse(r.url).path).suffix.lower()
    is_media = ctype.startswith(("image/", "video/", "audio/")) or ext in MEDIA_EXT \
        or ctype == "application/octet-stream" and ext in MEDIA_EXT
    if r.status_code >= 400 or not is_media:
        r.close()
        return 1, f"not a direct media file (HTTP {r.status_code}, {ctype or 'no type'})"
    name = Path(urlparse(r.url).path).name or "file"
    if not Path(name).suffix:
        guess = {"video/mp4": ".mp4", "image/jpeg": ".jpg", "image/png": ".png",
                 "image/webp": ".webp", "video/webm": ".webm", "audio/mpeg": ".mp3"}
        name += guess.get(ctype, "")
    r.close()
    if shutil.which("aria2c"):
        return run(["aria2c", "-x8", "-s8", "-k1M", "--user-agent", UA, "-d", str(dest),
                    "-o", name, c.url], c.timeout)
    with requests.get(c.url, headers=headers, stream=True, timeout=60) as r2:
        with open(dest / name, "wb") as f:
            for chunk in r2.iter_content(1 << 20):
                f.write(chunk)
    return 0, "ok"


def s_page_scrape(c: Ctx, dest: Path):
    """Last-resort HTML scrape: og:video / <video> / <source> / m3u8+mp4 links / big images."""
    import requests
    try:
        r = requests.get(c.url, headers={"User-Agent": UA, "Referer": c.url}, timeout=30)
    except Exception as e:
        return 1, str(e)
    if r.status_code >= 400:
        return 1, f"HTTP {r.status_code}"
    html = r.text
    found = []
    for pat in (
        r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video',
        r'<(?:video|source)[^>]+src=["\']([^"\']+)',
        r'["\'](https?:[^"\'\s<>\\]+?\.(?:m3u8|mp4|webm|mov)(?:\?[^"\'\s<>\\]*)?)["\']',
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    ):
        for m in re.findall(pat, html, re.I):
            u = urljoin(c.url, m.replace("\\/", "/").replace("&amp;", "&"))
            if u not in found:
                found.append(u)
    if not found:
        return 1, "no media links found in page"
    log(f"page scrape found {len(found)} candidate link(s)")
    got = 0
    for u in found[:30]:
        sub = Ctx(u, "", dest)
        sub.cookies, sub.max_height, sub.timeout = c.cookies, c.max_height, c.timeout
        if ".m3u8" in u or ".mpd" in u:
            rc, _ = run(_ytdlp_base(sub, dest) + ["--add-header", f"Referer:{c.url}", u], c.timeout)
        else:
            rc, _ = s_direct(sub, dest)
        got += rc == 0
    return (0 if got else 1), f"{got}/{len(found[:30])} links downloaded"


def s_you_get(c: Ctx, dest: Path):
    return run(["you-get", "-o", str(dest), c.url], c.timeout)


def s_ytdlp_gallery_playlist(c: Ctx, dest: Path):
    """yt-dlp playlist/profile mode, for pages that list many videos."""
    return run(_ytdlp_base(c, dest) + ["--yes-playlist", c.url], c.timeout)


STRATEGIES = {
    "gallery_dl": s_gallery_dl,
    "ytdlp": s_ytdlp,
    "ytdlp_impersonate": s_ytdlp_impersonate,
    "direct": s_direct,
    "ytdlp_generic": s_ytdlp_generic,
    "page_scrape": s_page_scrape,
    "you_get": s_you_get,
}


def choose_order(url: str):
    forced = os.environ.get("DL_TOOLS", "").strip()
    if forced:
        return [t.strip() for t in forced.split(",") if t.strip() in STRATEGIES]
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in MEDIA_EXT:
        first = ["direct", "ytdlp", "gallery_dl"]
    elif VIDEO_HOSTS.search(url):
        first = ["ytdlp", "gallery_dl", "ytdlp_impersonate"]
    else:
        first = ["gallery_dl", "ytdlp", "ytdlp_impersonate"]
    rest = ["direct", "ytdlp_generic", "page_scrape", "you_get"]
    order = []
    for t in first + rest:
        if t not in order:
            order.append(t)
    return order


# -------------------------------------------------------------------- driver
def download(url: str, out_dir, range_: str = "", need_video: bool = False):
    """Try tools in order. First clean success wins; otherwise the best partial result is kept.

    need_video=True: a tool only counts as a success if it produced at least one VIDEO file.
    (Otherwise gallery-dl "succeeding" with a thumbnail would stop the chain before yt-dlp ran.)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dl_"))
    ctx = Ctx(url, range_, work)
    attempts, best = [], None  # best = (bytes, tool, dir, files)

    for tool in choose_order(url):
        dest = work / tool
        dest.mkdir()
        t0 = time.time()
        log(f"=== trying {tool} ===")
        try:
            rc, tail = STRATEGIES[tool](ctx, dest)
        except Exception as e:  # a broken strategy must never kill the chain
            rc, tail = 1, f"{type(e).__name__}: {e}"
        files = media_files(dest)
        attempts.append({"tool": tool, "rc": rc, "files": len(files),
                         "seconds": round(time.time() - t0), "error": "" if rc == 0 else tail[-300:]})
        log(f"{tool}: rc={rc}, {len(files)} media file(s)")
        good = bool(files) and (not need_video or has_video(files))
        rank = (has_video(files), total_bytes(files))
        if files and (best is None or rank > best[0]):
            best = (rank, tool, dest, files)
        if rc == 0 and good:
            break  # clean success, stop here
        if rc == 0 and not good:
            log(f"{tool} said OK but produced {'no video' if files else 'no media'} - continuing")

    result = {"url": url, "winner": None, "files": [], "attempts": attempts}
    if best:
        _, tool, dest, files = best
        for f in files:
            target = out_dir / f.name
            n = 1
            while target.exists():
                target = out_dir / f"{f.stem}_{n}{f.suffix}"
                n += 1
            shutil.move(str(f), str(target))
            result["files"].append(str(target))
        result["winner"] = tool
        log(f"WINNER: {tool} -> {len(result['files'])} file(s)")
    else:
        log("ALL TOOLS FAILED for " + url)
    shutil.rmtree(work, ignore_errors=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: downloaders.py URL OUT_DIR [RANGE]")
    res = download(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    import json
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["files"] else 1)
