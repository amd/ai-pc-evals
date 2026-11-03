"""Save a pack of linked sources once, so every run and every machine gets identical copies.

usage:
  python tools/snapshot_sources.py capture <pack>              # fetch what can be fetched; list the rest
  python tools/snapshot_sources.py import <pack> <id> <file>   # add a copy saved by hand in a browser
  python tools/snapshot_sources.py lock <pack>                 # copy the manifest's hashes into the repo lock
  python tools/snapshot_sources.py verify <pack>               # check the local copy against the lock
  python tools/snapshot_sources.py zip <pack> <out.zip>        # bundle a pack for the test machine
  python tools/snapshot_sources.py unzip <in.zip>              # install a bundle there, then verify it

Packs are defined in knowledgework/portable/packs.json: {"<pack>": [{"id", "url", "kind"}]} where
kind is "page" (Chrome: PDF as a reader sees it, plus the rendered HTML), "file" (raw bytes) or
"manual" (never fetched: a person saves it in a browser and imports it).

Only sources whose terms allow it are fetched automatically: official, government and openly licensed
pages and files. A site whose terms forbid scraping or automated access (Reddit, X, LinkedIn,
perplexity.ai, ...) is kind "manual", and a pack that points "page" or "file" at one is rejected.
Sites that refuse automated access (bot protection, HTTP 403) are not worked around either: capture
lists them, the tester saves each one in a normal browser (Print > Save as PDF) and imports it.
Snapshots live in ~/.kw-eval/cache/snapshots/<pack>/ and are never committed: the
sources are linked, not bundled. Only their hashes are, in knowledgework/portable/snapshots.lock.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKS = REPO / "knowledgework" / "portable" / "packs.json"
#: The committed lock. KW_SNAPSHOT_LOCK points at another one for development packs (stand-ins for
#: pages not saved yet); every result then says DEV SNAPSHOTS and is not comparable.
LOCK = Path(os.environ.get("KW_SNAPSHOT_LOCK") or REPO / "knowledgework" / "portable" / "snapshots.lock.json")
ROOT = Path(os.environ.get("KW_SNAPSHOTS") or Path.home() / ".kw-eval" / "cache" / "snapshots")
CHROME = os.environ.get("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
UA = "ai-pc-evals/1.0 (+https://github.com/amd/ai-pc-evals)"
TIMEOUT_S = 120


class SnapshotError(RuntimeError):
    pass


KINDS = ("page", "file", "manual")
#: Sites whose terms forbid scraping or automated access: their sources can only be kind "manual".
NO_AUTOMATION = ("reddit.com", "redd.it", "x.com", "twitter.com", "linkedin.com", "perplexity.ai",
                 "facebook.com", "instagram.com", "threads.net", "tiktok.com")


def no_automation(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in NO_AUTOMATION)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack_sources(pack: str, packs_file: Path = PACKS) -> list[dict]:
    packs = json.loads(packs_file.read_text(encoding="utf-8"))
    if pack not in packs:
        raise SnapshotError(f"no pack {pack!r} in {packs_file}; known: {sorted(packs)}")
    sources = packs[pack]
    ids = [s["id"] for s in sources]
    if len(ids) != len(set(ids)):
        raise SnapshotError(f"pack {pack} repeats an id")
    for s in sources:
        if s.get("kind") not in KINDS or not s.get("url", "").startswith("https://"):
            raise SnapshotError(f"pack {pack}: bad source {s}")
        if s["kind"] != "manual" and no_automation(s["url"]):
            raise SnapshotError(f"pack {pack}: {s['id']} is on a site whose terms forbid automated access; "
                                "make it kind \"manual\" and save it in a browser")
    return sources


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read()


def chrome(*args: str) -> subprocess.CompletedProcess:
    """Headless Chrome on a throwaway profile, so the user's cookies and sessions are never used."""
    if not Path(CHROME).is_file() and not shutil.which(CHROME):
        raise SnapshotError(f"Chrome not found at {CHROME}; set CHROME_PATH")
    with tempfile.TemporaryDirectory(prefix="kw-chrome-") as profile:
        try:
            out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                                  f"--user-data-dir={profile}", "--virtual-time-budget=15000", *args],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise SnapshotError(f"Chrome did not finish in {TIMEOUT_S}s") from e
    if out.returncode != 0:
        raise SnapshotError(f"Chrome exited {out.returncode}: {out.stderr[-400:]}")
    if "Page load failed" in out.stderr:
        raise SnapshotError("the site refused the automated browser: " + out.stderr.strip().splitlines()[-1][-200:])
    return out


#: Text of bot-check interstitials. A snapshot of one of these looks saved but holds no source.
CHALLENGE_MARKERS = ("performing security verification", "verify you are human", "verifying you are human",
                     "checking your browser", "just a moment...", "attention required!", "access denied",
                     "enable javascript and cookies to continue", "you've been blocked by network security")


def challenge_page(html: str) -> str | None:
    low = html.lower()
    return next((m for m in CHALLENGE_MARKERS if m in low), None)


MIN_WORDS = 150


def visible_words(html: str) -> int:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return len(re.sub(r"<[^>]+>", " ", text).split())


def readable(html: str) -> None:
    """Refuse what isn't the page: a bot check, or a near-empty shell served to scripts."""
    marker = challenge_page(html)
    if marker:
        raise SnapshotError(f"the site served a bot check ({marker!r}) instead of the page")
    words = visible_words(html)
    if words < MIN_WORDS:
        raise SnapshotError(f"the site served a page with no readable content ({words} words)")


def capture_page(src: dict, dest: Path) -> list[Path]:
    pdf, html = dest / f"{src['id']}.pdf", dest / f"{src['id']}.html"
    # A site that refuses the automated browser has said no to automation: no other client is tried.
    dom = chrome("--dump-dom", src["url"]).stdout
    readable(dom)
    chrome(f"--print-to-pdf={pdf}", src["url"])
    if not pdf.is_file() or pdf.stat().st_size < 1000:
        pdf.unlink(missing_ok=True)
        raise SnapshotError(f"Chrome produced no usable PDF for {src['url']}")
    html.write_text(dom, encoding="utf-8")
    return [pdf, html]


def capture_file(src: dict, dest: Path) -> list[Path]:
    name = src.get("name") or src["url"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    out = dest / f"{src['id']}-{name}"
    out.write_bytes(http_get(src["url"]))
    return [out]


CAPTURE = {"page": capture_page, "file": capture_file}


def _entry(src: dict, f: Path, how: str) -> dict:
    return {"id": src["id"], "url": src["url"], "file": f.name, "how": how,
            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "sha256": sha256(f), "bytes": f.stat().st_size}


def _write_manifest(dest: Path, pack: str, entries: list[dict]) -> Path:
    manifest = dest / "manifest.json"
    manifest.write_text(json.dumps({"pack": pack, "files": entries}, indent=1), encoding="utf-8")
    return manifest


def capture(pack: str, root: Path = ROOT, packs_file: Path = PACKS, retry: bool = False) -> tuple[Path, list[str]]:
    """(manifest, ids that need a manual save). Sources that refuse automation are skipped, not forced.
    With *retry*, only sources not yet in the pack are fetched; saved ones are never touched."""
    sources = pack_sources(pack, packs_file)
    dest = root / pack
    entries: list[dict] = []
    if retry and (dest / "manifest.json").is_file():
        entries = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["files"]
        have = {e["id"] for e in entries}
        sources = [s for s in sources if s["id"] not in have]
    elif dest.exists() and any(dest.iterdir()):
        raise SnapshotError(f"{dest} already holds a capture; a pack is saved once. Use --retry to fetch only "
                            "the sources still missing, or move it aside to re-capture.")
    dest.mkdir(parents=True, exist_ok=True)
    manual = []
    for src in sources:
        if src["kind"] == "manual":
            print(f"needs a manual save: {src['id']} {src['url']} (not fetched automatically)", file=sys.stderr)
            manual.append(src["id"])
            continue
        try:
            files = CAPTURE[src["kind"]](src, dest)
        except (SnapshotError, OSError) as e:
            print(f"needs a manual save: {src['id']} {src['url']} ({e})", file=sys.stderr)
            manual.append(src["id"])
            continue
        entries += [_entry(src, f, src["kind"]) for f in files]
        print(f"saved {src['id']}")
    order = [s["id"] for s in pack_sources(pack, packs_file)]
    entries.sort(key=lambda e: order.index(e["id"]))
    return _write_manifest(dest, pack, entries), manual


def import_manual(pack: str, sid: str, file: Path, root: Path = ROOT, packs_file: Path = PACKS) -> Path:
    """Add a copy the tester saved in a normal browser; it keeps its pack id and URL."""
    sources = pack_sources(pack, packs_file)
    src = next((s for s in sources if s["id"] == sid), None)
    if src is None:
        raise SnapshotError(f"pack {pack} has no source {sid}")
    if not file.is_file():
        raise SnapshotError(f"{file} does not exist")
    dest = root / pack
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / "manifest.json"
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))["files"] if manifest_path.is_file() else []
    if any(e["id"] == sid for e in entries):
        raise SnapshotError(f"{sid} is already in {manifest_path}; a source is saved once")
    out = dest / f"{sid}{file.suffix.lower()}"
    shutil.copyfile(file, out)
    entries.append(_entry(src, out, "manual"))
    order = [s["id"] for s in sources]
    entries.sort(key=lambda e: order.index(e["id"]))
    return _write_manifest(dest, pack, entries)


def lock(pack: str, root: Path = ROOT, lock_file: Path = LOCK, packs_file: Path = PACKS) -> None:
    manifest = json.loads((root / pack / "manifest.json").read_text(encoding="utf-8"))
    missing = sorted({s["id"] for s in pack_sources(pack, packs_file)} - {e["id"] for e in manifest["files"]})
    if missing:
        raise SnapshotError(f"pack {pack} is missing {missing}; import them before locking")
    data = json.loads(lock_file.read_text(encoding="utf-8")) if lock_file.is_file() else {}
    data[pack] = {e["file"]: {"sha256": e["sha256"], "url": e["url"], "retrieved_at": e["retrieved_at"],
                              "how": e.get("how", "")} for e in manifest["files"]}
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def verify(pack: str, root: Path = ROOT, lock_file: Path = LOCK) -> list[str]:
    """Problems with the local copy of *pack*; empty when every locked file matches."""
    locked = json.loads(lock_file.read_text(encoding="utf-8")).get(pack) if lock_file.is_file() else None
    if not locked:
        return [f"pack {pack} is not in {lock_file}; capture and lock it first"]
    problems = []
    for name, want in sorted(locked.items()):
        f = root / pack / name
        if not f.is_file():
            problems.append(f"missing {f}")
        elif sha256(f) != want["sha256"]:
            problems.append(f"changed {f}: sha256 {sha256(f)}, locked {want['sha256']}")
    return problems


def make_zip(pack: str, out: Path, root: Path = ROOT) -> None:
    src = root / pack
    if not (src / "manifest.json").is_file():
        raise SnapshotError(f"{src} has no manifest.json; capture it first")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.iterdir()):
            z.write(f, f"{pack}/{f.name}")


def install_zip(bundle: Path, root: Path = ROOT) -> str:
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        packs = {n.split("/", 1)[0] for n in names}
        if len(packs) != 1 or any(".." in n or n.startswith("/") or "\\" in n for n in names):
            raise SnapshotError(f"{bundle} is not a single-pack bundle")
        pack = packs.pop()
        if (root / pack).exists() and any((root / pack).iterdir()):
            raise SnapshotError(f"{root / pack} already exists; move it aside first")
        z.extractall(root)
    return pack


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("pack")
    cap.add_argument("--retry", action="store_true", help="fetch only the sources not yet in the pack")
    for name in ("lock", "verify"):
        sub.add_parser(name).add_argument("pack")
    im = sub.add_parser("import")
    im.add_argument("pack")
    im.add_argument("id")
    im.add_argument("file", type=Path)
    z = sub.add_parser("zip")
    z.add_argument("pack")
    z.add_argument("out", type=Path)
    sub.add_parser("unzip").add_argument("bundle", type=Path)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.cmd == "capture":
            manifest, manual = capture(args.pack, retry=args.retry)
            print(f"wrote {manifest}")
            for sid in manual:
                print(f"  save {sid} in a browser (Print > Save as PDF), then: "
                      f"snapshot_sources.py import {args.pack} {sid} <file.pdf>")
            print(f"once every source is in and checked: snapshot_sources.py lock {args.pack}")
        elif args.cmd == "import":
            print(f"wrote {import_manual(args.pack, args.id, args.file)}")
        elif args.cmd == "lock":
            lock(args.pack)
            print(f"locked {args.pack} in {LOCK}")
        elif args.cmd == "zip":
            make_zip(args.pack, args.out)
            print(f"wrote {args.out}")
        else:
            pack = install_zip(args.bundle) if args.cmd == "unzip" else args.pack
            problems = verify(pack)
            for p in problems:
                print(f"FAIL {p}")
            if problems:
                return 1
            print(f"ok: {pack} matches {LOCK.name}")
    except SnapshotError as e:
        sys.exit(f"ERROR: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
