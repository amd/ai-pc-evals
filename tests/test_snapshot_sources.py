"""snapshot_sources.py offline: captures are faked; manifest, lock, verify and bundles are real."""
import json

import pytest

import snapshot_sources as ss

THREAD = [
    {"data": {"children": [{"kind": "t3", "data": {
        "title": "Strix Halo ROCm notes", "subreddit": "LocalLLaMA", "author": "op_user",
        "created_utc": 1778000000, "score": 120, "num_comments": 3,
        "selftext": "Prefill 450 tok/s, decode 38 tok/s on Q4_K_M with MTP."}}]}},
    {"data": {"children": [
        {"kind": "t1", "data": {"author": "commenter1", "created_utc": 1778003600, "score": 15,
                                "body": "Vulkan was faster for me.", "replies": {"data": {"children": [
                                    {"kind": "t1", "data": {"author": "op_user", "is_submitter": True,
                                                            "created_utc": 1778007200, "score": 4,
                                                            "body": "Which driver?", "replies": ""}},
                                    {"kind": "more", "data": {"count": 2}}]}}}},
        {"kind": "t1", "data": {"author": "[deleted]", "created_utc": None, "score": 1, "body": "[removed]",
                                "replies": ""}},
    ]}},
]


def test_reddit_markdown_keeps_post_and_comments_apart_and_counts_missing_replies():
    md = ss.reddit_markdown(THREAD, "https://www.reddit.com/r/LocalLLaMA/comments/x/")
    assert md.startswith("# Strix Halo ROCm notes")
    post, comments = md.split("## Comments")
    assert "Prefill 450 tok/s, decode 38 tok/s" in post and "u/op_user" in post
    assert "Vulkan was faster" in comments and "Vulkan" not in post
    assert "**u/op_user (original poster)**" in comments  # an OP reply is marked as such
    assert "[2 more replies not loaded in this snapshot]" in comments
    assert "date not shown" in comments  # a missing date is never guessed
    assert "2026-05-05" in post


def write_packs(tmp_path, sources):
    f = tmp_path / "packs.json"
    f.write_text(json.dumps({"P": sources}), encoding="utf-8")
    return f


def fake_capture(monkeypatch):
    def page(src, dest):
        p = dest / f"{src['id']}.pdf"
        p.write_bytes(f"pdf of {src['url']}".encode())
        return [p]
    monkeypatch.setitem(ss.CAPTURE, "page", page)


def test_capture_lock_verify_round_trip(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    packs = write_packs(tmp_path, [{"id": "P1", "url": "https://amd.com/halo", "kind": "page"},
                                   {"id": "P2", "url": "https://amd.com/playbooks", "kind": "page"}])
    root, lock = tmp_path / "snap", tmp_path / "lock.json"
    manifest = json.loads(ss.capture("P", root, packs)[0].read_text())
    assert [e["id"] for e in manifest["files"]] == ["P1", "P2"]
    assert all(len(e["sha256"]) == 64 and e["retrieved_at"].endswith("+00:00") for e in manifest["files"])
    ss.lock("P", root, lock, packs)
    assert ss.verify("P", root, lock) == []
    (root / "P" / "P1.pdf").write_bytes(b"changed upstream")
    (root / "P" / "P2.pdf").unlink()
    problems = ss.verify("P", root, lock)
    assert any(p.startswith("changed") and "P1.pdf" in p for p in problems)
    assert any(p.startswith("missing") and "P2.pdf" in p for p in problems)


def test_a_pack_is_captured_once(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    packs = write_packs(tmp_path, [{"id": "P1", "url": "https://amd.com/halo", "kind": "page"}])
    ss.capture("P", tmp_path / "snap", packs)
    with pytest.raises(ss.SnapshotError, match="saved once"):
        ss.capture("P", tmp_path / "snap", packs)


def test_bad_pack_definitions_are_rejected(tmp_path):
    with pytest.raises(ss.SnapshotError, match="bad source"):
        ss.pack_sources("P", write_packs(tmp_path, [{"id": "P1", "url": "http://insecure", "kind": "page"}]))
    with pytest.raises(ss.SnapshotError, match="repeats an id"):
        ss.pack_sources("P", write_packs(tmp_path, [{"id": "P1", "url": "https://a", "kind": "page"}] * 2))
    with pytest.raises(ss.SnapshotError, match="no pack"):
        ss.pack_sources("Q", write_packs(tmp_path, []))


def test_verify_without_a_lock_says_what_to_do(tmp_path):
    assert "capture and lock it first" in ss.verify("P", tmp_path, tmp_path / "missing.json")[0]


def test_bundle_installs_on_another_machine_and_verifies(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    packs = write_packs(tmp_path, [{"id": "P1", "url": "https://amd.com/halo", "kind": "page"}])
    here, there, lock = tmp_path / "here", tmp_path / "there", tmp_path / "lock.json"
    ss.capture("P", here, packs)
    ss.lock("P", here, lock, packs)
    ss.make_zip("P", tmp_path / "P.zip", here)
    assert ss.install_zip(tmp_path / "P.zip", there) == "P"
    assert ss.verify("P", there, lock) == []
    with pytest.raises(ss.SnapshotError, match="already exists"):
        ss.install_zip(tmp_path / "P.zip", there)


def test_reddit_falls_back_to_the_old_reddit_page_when_json_is_blocked(tmp_path, monkeypatch):
    seen = []

    def blocked(url):
        raise OSError("HTTP Error 403: Blocked")

    def page(src, dest):
        seen.append(src["url"])
        p = dest / f"{src['id']}.pdf"
        p.write_bytes(b"pdf")
        return [p]
    monkeypatch.setattr(ss, "http_get", blocked)
    monkeypatch.setattr(ss, "capture_page", page)
    out = ss.capture_reddit({"id": "R1", "url": "https://www.reddit.com/r/LocalLLM/comments/abc/x/"}, tmp_path)
    assert seen == ["https://old.reddit.com/r/LocalLLM/comments/abc/x/"]
    assert out[0].name == "R1.pdf"


def test_blocked_sources_are_listed_for_a_manual_save_then_imported(tmp_path, monkeypatch):
    def page(src, dest):
        if "amd.com" in src["url"]:
            raise ss.SnapshotError("the site refused the automated browser")
        p = dest / f"{src['id']}.pdf"
        p.write_bytes(b"pdf")
        return [p]
    monkeypatch.setitem(ss.CAPTURE, "page", page)
    packs = write_packs(tmp_path, [{"id": "P1", "url": "https://www.amd.com/halo", "kind": "page"},
                                   {"id": "P3", "url": "https://www.perplexity.ai/hub/x", "kind": "page"}])
    root, lock = tmp_path / "snap", tmp_path / "lock.json"
    manifest, manual = ss.capture("P", root, packs)
    assert manual == ["P1"]
    with pytest.raises(ss.SnapshotError, match=r"missing \['P1'\]"):
        ss.lock("P", root, lock, packs)
    saved = tmp_path / "Ryzen AI Halo - AMD.pdf"
    saved.write_bytes(b"%PDF saved by hand")
    ss.import_manual("P", "P1", saved, root, packs)
    entries = json.loads(manifest.read_text())["files"]
    assert [(e["id"], e["file"], e["how"]) for e in entries] == [("P1", "P1.pdf", "manual"), ("P3", "P3.pdf", "page")]
    assert entries[0]["url"] == "https://www.amd.com/halo"
    with pytest.raises(ss.SnapshotError, match="saved once"):
        ss.import_manual("P", "P1", saved, root, packs)
    with pytest.raises(ss.SnapshotError, match="no source P9"):
        ss.import_manual("P", "P9", saved, root, packs)
    ss.lock("P", root, lock, packs)
    assert ss.verify("P", root, lock) == []


def test_a_reddit_json_saved_in_a_browser_imports_as_markdown(tmp_path):
    packs = tmp_path / "packs.json"
    packs.write_text(json.dumps({"R": [{"id": "R1", "url": "https://www.reddit.com/r/x/comments/1/t/",
                                        "kind": "reddit"}]}), encoding="utf-8")
    saved = tmp_path / "thread.json"
    saved.write_text(json.dumps(THREAD), encoding="utf-8")
    manifest = ss.import_manual("R", "R1", saved, tmp_path / "snap", packs)
    files = [e["file"] for e in json.loads(manifest.read_text())["files"]]
    assert files == ["R1.md", "R1.json"]
    assert "[2 more replies not loaded in this snapshot]" in (tmp_path / "snap" / "R" / "R1.md").read_text("utf-8")
    bad = tmp_path / "login.json"
    bad.write_text('{"error": 403}', encoding="utf-8")
    packs.write_text(json.dumps({"R": [{"id": "R2", "url": "https://www.reddit.com/r/x/comments/2/t/",
                                        "kind": "reddit"}]}), encoding="utf-8")
    with pytest.raises(ss.SnapshotError, match="not a Reddit thread"):
        ss.import_manual("R", "R2", bad, tmp_path / "snap", packs)
    assert not (tmp_path / "snap" / "R" / "R2.json").exists()


def test_chrome_runs_on_a_throwaway_profile(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return ss.subprocess.CompletedProcess(cmd, 0, "<html></html>", "")
    monkeypatch.setattr(ss.subprocess, "run", run)
    monkeypatch.setattr(ss, "CHROME", __file__)  # any existing file
    ss.chrome("--dump-dom", "https://example.com")
    assert any(a.startswith("--user-data-dir=") and "kw-chrome-" in a for a in seen["cmd"])


def test_a_bot_check_page_is_never_saved_as_the_source(monkeypatch, tmp_path):
    cloudflare = ("<html><body>www.perplexity.ai Performing security verification This website uses a "
                  "security service to protect against malicious bots.</body></html>")
    monkeypatch.setattr(ss, "chrome", lambda *a: ss.subprocess.CompletedProcess(a, 0, cloudflare, ""))
    with pytest.raises(ss.SnapshotError, match="bot check"):
        ss.capture_page({"id": "P3", "url": "https://www.perplexity.ai/hub/blog/x"}, tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert ss.challenge_page("<h1>AMD AI Playbooks</h1><p>Start building</p>") is None


def test_a_refused_page_load_is_an_error_not_a_blank_snapshot(monkeypatch):
    monkeypatch.setattr(ss.subprocess, "run", lambda cmd, **kw: ss.subprocess.CompletedProcess(
        cmd, 0, "", "ERROR: Page load failed: net::ERR_HTTP2_PROTOCOL_ERROR"))
    monkeypatch.setattr(ss, "CHROME", __file__)
    with pytest.raises(ss.SnapshotError, match="refused the automated browser"):
        ss.chrome("--dump-dom", "https://www.amd.com/")
