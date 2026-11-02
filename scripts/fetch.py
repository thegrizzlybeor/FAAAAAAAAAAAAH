#!/usr/bin/env python3
"""
fetch.py <url> <index>

Downloads everything behind one URL, compresses each file according to what it
actually is (video / image / animated image / audio / other) and writes the
result to  out/<index>-<slug>/ . Every output file is kept under MAX_MB so it
can be pushed to GitHub (hard limit is 100 MB per file).

Tuning is done with environment variables (all optional):
  MAX_MB=90            max size of any single output file
  MAX_HEIGHT=720       videos are scaled DOWN to this height (never up)
  VIDEO_CODEC=h265     h265 (smaller) or h264 (plays everywhere)
  IMAGE_QUALITY=82     WebP quality
  IMAGE_MAX_SIDE=3000  images larger than this are scaled down
  BUNDLE_IMAGES_OVER=0 more than this many images -> packed into images.zip (0 = always)
  COOKIES              contents of a Netscape cookies.txt (optional)
  TOOL                 gallery | items | ytdlp-range | chain   (set by the planner)
  EXPECTED             how many files/videos this job must deliver (set by the planner)
  PLAN_FILE            plan/plan.json (needed for TOOL=items)
  DEADLINE_MIN=300     after this many minutes nothing is re-encoded any more: the original
                       is kept (and split) instead, so no video is lost to the 6 h job limit
  DEDUP=0              1 = skip files that look identical (OFF by default: nothing is skipped)

Every job writes reports/<job>.json (expected / downloaded / failed URLs). verify.py reads them.
The job exits non-zero if ANYTHING is missing, so an incomplete job is never shown as green.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import downloaders as dl   # multi-tool fallback chain (yt-dlp, gallery-dl, impersonation, scrape ...)

URL = sys.argv[1].strip()
IDX = int(sys.argv[2]) if len(sys.argv) > 2 else 1
RANGE = sys.argv[3].strip() if len(sys.argv) > 3 else ""   # gallery-dl --range slice of an album
PART = sys.argv[4].strip() if len(sys.argv) > 4 else ""    # p01, p02 ... for big albums

MB = 1024 * 1024
MAX_BYTES = int(float(os.getenv("MAX_MB", "90")) * MB)
MAX_H = int(os.getenv("MAX_HEIGHT", "720"))
CODEC = os.getenv("VIDEO_CODEC", "h265").lower()
IMG_Q = int(os.getenv("IMAGE_QUALITY", "82"))
IMG_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "3000"))
BUNDLE_OVER = int(os.getenv("BUNDLE_IMAGES_OVER", "0"))   # 0 = images are ALWAYS zipped
TOOL = (os.getenv("TOOL") or "gallery").strip()
EXPECTED = int(os.getenv("EXPECTED") or 0)
PLAN_FILE = Path(os.getenv("PLAN_FILE") or "plan/plan.json")
T0 = time.time()
DEADLINE = T0 + float(os.getenv("DEADLINE_MIN") or 300) * 60
DEDUP = os.getenv("DEDUP", "0") == "1"
JOB = f"{IDX}-{PART}" if PART else f"{IDX}"
REPORTS = Path("reports")


def time_left():
    return DEADLINE - time.time()

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".avif", ".heic"}
VID_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts", ".wmv", ".3gp"}
AUD_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
JUNK_EXT = {".json", ".part", ".ytdl", ".tmp", ".description", ".txt", ".html"}


def slugify(s, n=60):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._-")[:n] or "file"


_u = urlparse(URL)
NAME = "-".join(x for x in (f"{IDX:02d}", PART, slugify(_u.netloc + _u.path, 50)) if x)
WORK = Path("work") / NAME
OUT = Path("out") / NAME
IMGDIR = WORK / "img"
COOKIES = Path("cookies.txt")

LOG_TAIL = []


def log(msg=""):
    print(msg, flush=True)


def run(cmd, timeout=None):
    try:
        p = subprocess.run([str(c) for c in cmd], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ff(args, timeout=None):
    timeout = timeout or max(60, time_left())
    rc, out = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], timeout)
    if rc != 0:
        log(f"   ffmpeg failed: {out.strip()[-300:]}")
    return rc == 0


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(p)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def unique(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    i = 2
    while True:
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def mb(n):
    return f"{n / MB:.1f} MB"


# --------------------------------------------------------------------------
# 1. DOWNLOAD  (gallery-dl only)
# --------------------------------------------------------------------------
def media_files(d: Path):
    return sorted(p for p in d.rglob("*")
                  if p.is_file() and p.suffix.lower() not in JUNK_EXT)


def run_gallery(dest: Path, rng: str, timeout):
    ext = Path(urlparse(URL).path).suffix.lower()
    target = URL
    if ext in IMG_EXT | VID_EXT | AUD_EXT:  # plain direct link -> gallery-dl's direct:// extractor
        target = "direct://" + URL
    g = ["gallery-dl", "-d", dest, "--retries", "6", "--sleep-request", "0.5-1.5"]
    if COOKIES.exists():
        g += ["--cookies", COOKIES]
    if rng:
        g += ["--range", rng]
    rc, out = run(g + [target], timeout)
    log(out.strip()[-1200:])
    LOG_TAIL.append(out.strip()[-600:])
    return rc


def download_gallery():
    """gallery-dl for this job's slice. Re-runs while files are missing (it skips what it has)."""
    dest = WORK / "dl"
    dest.mkdir(parents=True, exist_ok=True)
    if Path(URL).exists():  # local file (handy for testing)
        shutil.copy(URL, dest)
        return media_files(dest)
    tries = 0
    while True:
        tries += 1
        log(f">> gallery-dl (pass {tries})" + (f", range {RANGE}" if RANGE else ""))
        budget = max(5.0, min(150.0, time_left() / 60 - 60))
        run_gallery(dest, RANGE, budget * 60)
        files = media_files(dest)
        if not EXPECTED or len(files) >= EXPECTED or tries >= 3 or time_left() < 3600:
            return files
        log(f"   only {len(files)} of {EXPECTED} files -> trying again")


def fetch_one(u: str, n: int):
    """One item URL through the whole fallback chain (2 rounds). Returns (files, why_failed)."""
    d = WORK / "items" / f"{n:05d}"
    need_video = Path(urlparse(u).path).suffix.lower() not in IMG_EXT
    why = ""
    for attempt in (1, 2):
        shutil.rmtree(d, ignore_errors=True)
        os.environ["DL_TIMEOUT_MIN"] = str(max(5, min(45, int(time_left() / 60) - 15)))
        log(f">> item {n}: {u}  (round {attempt})")
        try:
            res = dl.download(u, d, need_video=need_video)
        except Exception as e:
            res = {"files": [], "attempts": [{"tool": "download()", "error": f"{type(e).__name__}: {e}"}]}
        files = [Path(f) for f in res["files"]]
        if files:
            if need_video and not dl.has_video(files):
                NOTES.append(f"item {n} ({u}): no video found, only {len(files)} other file(s)")
            return files, ""
        why = "; ".join(f"{x.get('tool', '?')}: {str(x.get('error', ''))[-120:]}" for x in res["attempts"])[:600]
        time.sleep(10)
    return [], why


# --------------------------------------------------------------------------
# 2. COMPRESS
# --------------------------------------------------------------------------
def sniff(p: Path) -> str:
    """Identify a file by its first bytes, not its name. '' = unknown."""
    try:
        with open(p, "rb") as f:
            head = f.read(64)
        size = p.stat().st_size
    except OSError:
        return ""
    if (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or (head[:2] == b"BM" and head[6:10] == b"\0\0\0\0")
            or head[:4] in (b"II*\x00", b"MM\x00*")
            or (head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"heim", b"mif1", b"msf1", b"avif"))):
        return "image"
    if size < 512 * 1024 and head.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml")):
        return "junk"   # an error page saved under a media name
    return ""


def kind(p: Path) -> str:
    s = sniff(p)
    if s:
        return s
    e = p.suffix.lower()
    streams = probe(p).get("streams", [])          # what is REALLY inside
    still = {"mjpeg", "png", "bmp", "webp", "tiff", "gif", "jpeg2000", "heic", "avif"}
    vids = [x for x in streams if x.get("codec_type") == "video"]
    if vids and not all(x.get("codec_name") in still for x in vids):
        return "video"
    if e in IMG_EXT or vids:
        return "image"
    if any(x.get("codec_type") == "audio" for x in streams):
        return "audio"
    if e in VID_EXT:
        return "video"
    if e in AUD_EXT:
        return "audio"
    return "other"


def fingerprint(p: Path):
    """size + hash of head and tail: cheap duplicate detection even for huge files."""
    h, size = hashlib.sha1(), p.stat().st_size
    with open(p, "rb") as f:
        h.update(f.read(4 * MB))
        if size > 8 * MB:
            f.seek(-4 * MB, 2)
            h.update(f.read(4 * MB))
    return size, h.hexdigest()


def split_file(path: Path, dest_dir: Path):
    """Raw split for files that can't be re-encoded. Rejoin: cat name.part* > name"""
    outs, block = [], 64 * MB
    with open(path, "rb") as f:
        i = 1
        while True:
            part = dest_dir / f"{path.name}.part{i:03d}"
            written = 0
            with open(part, "wb") as o:
                while written < MAX_BYTES:
                    data = f.read(min(block, MAX_BYTES - written))
                    if not data:
                        break
                    o.write(data)
                    written += len(data)
            if written == 0:
                part.unlink()
                break
            outs.append(part)
            i += 1
    return outs


def keep_as_is(src: Path):
    if src.stat().st_size > MAX_BYTES:
        return split_file(src, OUT)
    dst = unique(OUT / src.name)
    if WORK in src.parents:   # move instead of copy: no double disk use
        shutil.move(src, dst)
    else:
        shutil.copy(src, dst)
    return [dst]


def split_video(path: Path):
    dur = float(probe(path).get("format", {}).get("duration") or 0)
    size = path.stat().st_size
    if dur <= 0:
        return [path]
    seg = max(10, int(dur * MAX_BYTES * 0.85 / size))
    pattern = OUT / f"{path.stem}_part%02d.mp4"
    if ff(["-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
           "-segment_time", seg, "-reset_timestamps", "1", pattern]):
        path.unlink()
        return sorted(OUT.glob(f"{path.stem}_part*.mp4"))
    return [path]


def compress_audio(src: Path):
    dst = unique(OUT / f"{slugify(src.stem)}.m4a")
    if ff(["-i", src, "-vn", "-c:a", "aac", "-b:a", "96k", dst]) \
            and dst.stat().st_size < src.stat().st_size:
        return [dst]
    dst.unlink(missing_ok=True)
    return keep_as_is(src)


def compress_video(src: Path):
    info = probe(src)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        return compress_audio(src) if any(s.get("codec_type") == "audio" for s in streams) \
            else keep_as_is(src)

    h = int(v.get("height") or 0)
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    vbr = int(v.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
    size = src.stat().st_size
    dst = unique(OUT / f"{slugify(src.stem)}.mp4")

    # Already small + efficient? Don't waste quality re-encoding it.
    if v.get("codec_name") in ("h264", "hevc") and 0 < h <= MAX_H \
            and 0 < vbr < 900_000 and size <= MAX_BYTES:
        log("   already efficient -> remux only")
        if ff(["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
               "-movflags", "+faststart", dst]):
            return [dst]

    def encode(rate, height, target=None):
        target = target or dst
        vf = f"scale=-2:'min({height},trunc(ih/2)*2)'"
        # long videos get a faster preset so a job never runs out of time
        if CODEC == "h265":
            preset = "fast" if dur <= 1800 else "veryfast" if dur <= 7200 else "ultrafast"
            codec = ["-c:v", "libx265", "-preset", preset, "-tag:v", "hvc1",
                     "-x265-params", "log-level=error"]
        else:
            preset = "medium" if dur <= 1800 else "veryfast" if dur <= 7200 else "superfast"
            codec = ["-c:v", "libx264", "-preset", preset, "-profile:v", "high"]
        return ff(["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
                   *codec, *rate, "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                   "-movflags", "+faststart", target])

    if time_left() < 300:  # job deadline close: don't start a long encode, keep the original
        log("   out of time -> keeping original (no re-encode)")
        return keep_as_is(src)

    crf = "28" if CODEC == "h265" else "26"
    if not encode(["-crf", crf], MAX_H):
        dst.unlink(missing_ok=True)
        log("   encode failed -> keeping original")
        return keep_as_is(src)

    # Too big for GitHub? Re-encode to hit a bitrate budget instead.
    if dst.stat().st_size > MAX_BYTES and dur > 0:
        vb = int(MAX_BYTES * 8 * 0.93 / dur - 96_000)
        if vb >= 120_000:
            height = MAX_H if vb >= 350_000 else min(MAX_H, 480)
            log(f"   {mb(dst.stat().st_size)} too big -> targeting {vb // 1000} kbps @ {height}p")
            tmp = dst.with_name(dst.stem + "_retry.mp4")
            if encode(["-b:v", str(vb), "-maxrate", str(int(vb * 1.4)),
                       "-bufsize", str(vb * 2)], height, tmp) and tmp.exists():
                tmp.replace(dst)
            else:
                tmp.unlink(missing_ok=True)
    if dst.stat().st_size > MAX_BYTES:
        log("   still too big -> splitting into playable parts")
        return split_video(dst)
    return [dst]


def animation_to_mp4(src: Path, dst: Path, im) -> bool:
    """Decode frames with Pillow (works for animated WebP/APNG) and encode with ffmpeg."""
    frames = WORK / f"frames_{slugify(src.stem, 30)}"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    try:
        total_ms, n = 0, 0
        for i in range(im.n_frames):
            im.seek(i)
            total_ms += im.info.get("duration") or 100
            im.convert("RGB").save(frames / f"{i:05d}.png")
            n += 1
        fps = max(1.0, min(60.0, n * 1000.0 / max(total_ms, 1)))
        return ff(["-framerate", f"{fps:.3f}", "-i", frames / "%05d.png",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                   "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p", "-an",
                   "-movflags", "+faststart", dst])
    finally:
        shutil.rmtree(frames, ignore_errors=True)


def compress_image(src: Path):
    """Returns a file inside IMGDIR (webp, or mp4 for animations, or the original)."""
    IMGDIR.mkdir(parents=True, exist_ok=True)
    stem = slugify(src.stem)

    def original():
        dst = unique(IMGDIR / f"{stem}{src.suffix.lower()}")
        shutil.copy(src, dst)
        return dst

    try:
        from PIL import Image, ImageOps
        try:  # optional: lets Pillow open .heic/.avif
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        im = Image.open(src)
        if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
            dst = unique(IMGDIR / f"{stem}.mp4")  # animated gif/webp -> mp4 is ~10x smaller
            ok = False
            if src.suffix.lower() != ".webp":  # ffmpeg can't decode animated WebP
                ok = ff(["-i", src, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                         "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p", "-an",
                         "-movflags", "+faststart", dst])
            if not ok:
                dst.unlink(missing_ok=True)
                ok = animation_to_mp4(src, dst, im)
            if ok and dst.exists() and dst.stat().st_size < src.stat().st_size:
                return dst
            dst.unlink(missing_ok=True)
            return original()
        im = ImageOps.exif_transpose(im)
        im.thumbnail((IMG_SIDE, IMG_SIDE))  # only ever shrinks
        alpha = "A" in im.getbands() or "transparency" in im.info
        im = im.convert("RGBA" if alpha else "RGB")
        dst = unique(IMGDIR / f"{stem}.webp")
        im.save(dst, "WEBP", quality=IMG_Q, method=6)
        if dst.stat().st_size >= src.stat().st_size:  # webp didn't help -> keep original
            dst.unlink()
            return original()
        return dst
    except Exception as e:
        log(f"   image fallback ({e.__class__.__name__}) -> keeping original")
        return original()


def bundle_images(imgs):
    groups, cur, cur_size = [], [], 0
    for p in imgs:
        s = p.stat().st_size
        if cur and cur_size + s > MAX_BYTES:
            groups.append(cur)
            cur, cur_size = [], 0
        cur.append(p)
        cur_size += s
    if cur:
        groups.append(cur)
    outs = []
    for i, g in enumerate(groups, 1):
        name = "images.zip" if len(groups) == 1 else f"images_{i:02d}.zip"
        zp = OUT / name
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:  # already compressed
            for p in g:
                z.write(p, p.name)
        outs.append(zp)
    return outs


# --------------------------------------------------------------------------
NOTES = []
S = {"results": [], "images": [], "orig": 0, "seen": set(), "junk": [], "dups": []}


def process(files):
    """Compress / store the given downloaded files. Returns how many were kept."""
    kept = 0
    for p in files:
        k = kind(p)
        orig = p.stat().st_size
        if k == "junk":
            log(f"-- skip  {p.name} (error page, not media)")
            S["junk"].append(p.name)
            p.unlink(missing_ok=True)
            continue
        if DEDUP:
            fp = fingerprint(p)
            if fp in S["seen"]:
                log(f"-- skip  {p.name} (duplicate)")
                S["dups"].append(p.name)
                p.unlink(missing_ok=True)
                kept += 1
                continue
            S["seen"].add(fp)
        S["orig"] += orig
        log(f"-- {k:5} {p.name} ({mb(orig)})")
        try:
            if k == "video":
                outs = compress_video(p)
            elif k == "audio":
                outs = compress_audio(p)
            elif k == "image":
                img = compress_image(p)
                if img.suffix == ".mp4":
                    outs = keep_as_is(img)
                else:
                    S["images"].append(img)
                    p.unlink(missing_ok=True)
                    kept += 1
                    continue
            else:
                outs = keep_as_is(p)
        except Exception as e:  # never lose a file because of a bug
            log(f"   !! {e.__class__.__name__}: {e} -> keeping original")
            outs = keep_as_is(p) if p.exists() else []
        S["results"] += outs
        kept += 1
        p.unlink(missing_ok=True)   # free disk before the next file
    return kept


def run_items():
    plan = json.loads(PLAN_FILE.read_text())
    link = next(l for l in plan["links"] if l["idx"] == IDX)
    a, b = (int(x) for x in RANGE.split("-"))
    got, failed = 0, []
    for n in range(a, b + 1):
        u = link["items"][n - 1]
        if time_left() < 600:
            failed.append({"n": n, "url": u, "why": "job deadline reached before this item was started"})
            continue
        files, why = fetch_one(u, n)
        if not files:
            failed.append({"n": n, "url": u, "why": why or "no file downloaded"})
            continue
        if process(files) == 0:
            failed.append({"n": n, "url": u, "why": "only error pages / junk came back"})
            continue
        got += 1
    return got, failed


def run_ytdlp_range():
    d = WORK / "ytrange"
    os.environ["DL_TOOLS"] = "ytdlp,ytdlp_impersonate"
    os.environ["DL_TIMEOUT_MIN"] = str(max(5, min(150, int(time_left() / 60) - 60)))
    res = dl.download(URL, d, range_=RANGE, need_video=True)
    files = [Path(f) for f in res["files"]]
    got = process(files)
    failed = []
    if EXPECTED and got < EXPECTED:
        failed.append({"n": RANGE, "url": URL, "why": f"only {got} of {EXPECTED} entries of range {RANGE} downloaded"})
    return got, failed


def run_chain():
    files, why = fetch_one(URL, 0)
    if not files:
        return 0, [{"n": 0, "url": URL, "why": why or "no file downloaded"}]
    got = process(files)
    return got, ([] if got else [{"n": 0, "url": URL, "why": "only error pages / junk came back"}])


def run_gallery_job():
    files = download_gallery()
    got = process(files)
    failed = []
    if EXPECTED and got < EXPECTED:
        failed.append({"n": RANGE or "all", "url": URL,
                       "why": f"only {got} of {EXPECTED} files obtained" + (f" (range {RANGE})" if RANGE else "")})
    return got, failed


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    REPORTS.mkdir(exist_ok=True)
    if os.getenv("COOKIES"):
        COOKIES.write_text(os.environ["COOKIES"])

    log(f"=== [{NAME}] {URL}  tool={TOOL} expected={EXPECTED or '?'} range={RANGE or '-'}")
    runner = {"items": run_items, "ytdlp-range": run_ytdlp_range,
              "chain": run_chain}.get(TOOL, run_gallery_job)
    try:
        got, failed = runner()
    except Exception as e:  # never die silently: the report must exist
        log(f"!!! {e.__class__.__name__}: {e}")
        got, failed = 0, [{"n": "?", "url": URL, "why": f"job crashed: {e.__class__.__name__}: {e}"}]

    results, images = S["results"], S["images"]
    if images:
        big = [p for p in images if p.stat().st_size > MAX_BYTES]
        images = [p for p in images if p not in big]
        for img in big:
            results += keep_as_is(img)
        if len(images) > BUNDLE_OVER:
            results += bundle_images(images)
        else:
            for img in images:
                dst = unique(OUT / img.name)
                shutil.copy(img, dst)
                results.append(dst)

    if got == 0:
        (OUT / "FAILED.txt").write_text(f"Nothing could be downloaded from:\n{URL}\n\n"
                                        + "\n---\n".join(LOG_TAIL))
        log("!!! nothing downloaded")

    final = sum(p.stat().st_size for p in results)
    orig_total = S["orig"]
    out_of_time = any("deadline" in f["why"] for f in failed)
    report = {"job": JOB, "idx": IDX, "part": PART, "range": RANGE, "url": URL, "tool": TOOL,
              "expected": EXPECTED, "downloaded": got, "failed": failed, "notes": NOTES,
              "skipped_junk": S["junk"], "skipped_duplicates": S["dups"], "out_of_time": out_of_time,
              "original_bytes": orig_total, "final_bytes": final,
              "files": [{"name": p.name, "bytes": p.stat().st_size} for p in results]}
    (OUT / "_report.json").write_text(json.dumps(report, indent=2))
    (REPORTS / f"{JOB}.json").write_text(json.dumps(report, indent=2))

    line = (f"| {NAME} | {got}/{EXPECTED or '?'} | {len(results)} | {mb(orig_total)} | {mb(final)} | "
            f"{(1 - final / orig_total) * 100 if orig_total else 0:.0f}% |")
    log(f"=== done: {got}/{EXPECTED or '?'} item(s), {len(results)} output file(s), {mb(orig_total)} -> {mb(final)}")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("| Job | Got/Expected | Output files | Before | After | Saved |\n|---|---|---|---|---|---|\n" + line + "\n")
            for x in failed:
                f.write(f"\n- MISSING `{x['url']}` - {x['why']}\n")

    complete = not failed and (got >= EXPECTED if EXPECTED else got > 0)
    if not complete:
        log(f"!!! INCOMPLETE: {len(failed)} problem(s); see reports/{JOB}.json")
    sys.exit(0 if complete else 1)


if __name__ == "__main__":
    main()
