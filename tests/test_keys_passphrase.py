# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Rotating the answers passphrase and copying it to the clipboard, on temp bundles with a fake keyring."""

import hashlib
import json
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

import isolation_helpers as h  # noqa: I001  (puts tools/ on sys.path)
import keys

NEW = "a brand new passphrase 4419 zx"


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)

    @property
    def stored(self):
        return self.values.get((keys.KEYRING_SERVICE, keys.KEYRING_USER))


@pytest.fixture
def fake_keyring(monkeypatch):
    monkeypatch.delenv(keys.PASSPHRASE_ENV, raising=False)
    previous = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = h.make_repo(tmp_path)
    monkeypatch.setattr(keys, "REPO", repo)
    return repo


def _snapshot(repo: Path) -> dict:
    return {rel: (repo / rel).read_bytes()
            for rel in ("secrets/answers.enc", "secrets/manifest.json", "knowledgework/tasks.public.json")}


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def test_rotate_round_trip_keeps_the_answers_and_only_the_bundle_hash_changes(repo, tmp_path, fake_keyring):
    before = json.loads((h.decrypt(repo, tmp_path / "before") / "canaries.json").read_text(encoding="utf-8"))
    old = _snapshot(repo)
    problems = keys.rotate(h.PASSPHRASE, NEW, repo=repo, scrypt=h.FAST_SCRYPT)
    assert problems == []
    assert fake_keyring.stored == NEW
    with pytest.raises(keys.WrongPassphrase):
        h.decrypt(repo, tmp_path / "old")
    out = keys.decrypt_to(tmp_path / "after", NEW, bundle=repo / "secrets" / "answers.enc", repo=repo)
    assert json.loads((out / "canaries.json").read_text(encoding="utf-8")) == before
    tasks = json.loads((out / "tasks.json").read_text(encoding="utf-8"))
    assert [t["id"] for t in tasks["tasks"]] == [t["id"] for t in h.TASKS["tasks"]]
    new = _snapshot(repo)
    assert new["knowledgework/tasks.public.json"] == old["knowledgework/tasks.public.json"]
    assert new["secrets/answers.enc"] != old["secrets/answers.enc"]
    old_manifest = json.loads(old["secrets/manifest.json"])
    new_manifest = json.loads(new["secrets/manifest.json"])
    assert new_manifest.pop("bundle_sha256") == keys.sha256_bytes(new["secrets/answers.enc"])
    old_manifest.pop("bundle_sha256")
    assert new_manifest == old_manifest  # fingerprints and canary digests do not depend on the passphrase
    assert keys.check(repo=repo, deep_passphrase=NEW) == []
    assert NEW.encode() not in _all_bytes(repo)
    assert not list((repo / "secrets").glob("*.tmp"))


@pytest.mark.parametrize("new, why", [
    ("short one 1", "too short"),
    ("aaaaaaaaaaaaaaaaaaaaaaaaaaaa", "too few"),
    (" " + NEW, "whitespace"),
    (h.PASSPHRASE, "is the old one"),
])
def test_rotate_refuses_a_weak_or_unchanged_passphrase_and_changes_nothing(repo, fake_keyring, new, why):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    old = _snapshot(repo)
    with pytest.raises(keys.KeysError, match=why):
        keys.rotate(h.PASSPHRASE, new, repo=repo, scrypt=h.FAST_SCRYPT)
    assert _snapshot(repo) == old
    assert fake_keyring.stored == h.PASSPHRASE


def test_rotate_with_the_wrong_old_passphrase_changes_nothing(repo, fake_keyring):
    old = _snapshot(repo)
    with pytest.raises(keys.WrongPassphrase):
        keys.rotate("not the passphrase at all", NEW, repo=repo, scrypt=h.FAST_SCRYPT)
    assert _snapshot(repo) == old
    assert fake_keyring.stored is None


def test_a_keyring_failure_puts_the_old_bundle_and_passphrase_back(repo, tmp_path):
    old = _snapshot(repo)
    calls = []

    def store(value):
        calls.append(value)
        if value == NEW:
            raise keys.KeysError("credential store is locked")

    with pytest.raises(keys.KeysError, match="locked"):
        keys.rotate(h.PASSPHRASE, NEW, repo=repo, scrypt=h.FAST_SCRYPT, store=store)
    assert _snapshot(repo) == old
    assert calls == [NEW, h.PASSPHRASE]
    assert h.decrypt(repo, tmp_path / "plain").is_dir()
    assert not list((repo / "secrets").glob("*.tmp"))


def test_a_failure_writing_the_manifest_puts_the_old_bundle_back(repo, monkeypatch, fake_keyring):
    old = _snapshot(repo)

    def broken(_manifest):
        raise OSError("disk full")

    monkeypatch.setattr(keys, "_manifest_bytes", broken)
    with pytest.raises(OSError):
        keys.rotate(h.PASSPHRASE, NEW, repo=repo, scrypt=h.FAST_SCRYPT)
    assert _snapshot(repo) == old
    assert fake_keyring.stored is None
    assert keys.check(repo=repo, deep_passphrase=h.PASSPHRASE) == []


def test_rotate_refuses_a_bundle_that_does_not_match_its_manifest(repo, fake_keyring):
    manifest = json.loads((repo / "secrets" / "manifest.json").read_text(encoding="utf-8"))
    manifest["bundle_sha256"] = "0" * 64
    (repo / "secrets" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    old = _snapshot(repo)
    with pytest.raises(keys.KeysError, match="does not match"):
        keys.rotate(h.PASSPHRASE, NEW, repo=repo, scrypt=h.FAST_SCRYPT)
    assert _snapshot(repo) == old


def test_rotate_command_reads_the_new_passphrase_twice_and_never_prints_either(repo, fake_keyring, monkeypatch, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    answers = iter([NEW, NEW])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert keys.main(["rotate"]) == 0
    assert fake_keyring.stored == NEW
    out = capsys.readouterr()
    assert "decrypted OK" in out.out and "copy-passphrase" in out.out
    for secret in (NEW, h.PASSPHRASE):
        assert secret not in out.out + out.err


def test_rotate_command_refuses_two_different_entries(repo, fake_keyring, monkeypatch, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    old = _snapshot(repo)
    answers = iter([NEW, NEW + "x"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert keys.main(["rotate"]) == 2
    assert "differ" in capsys.readouterr().err
    assert _snapshot(repo) == old
    assert fake_keyring.stored == h.PASSPHRASE


def test_rotate_generate_stores_a_strong_passphrase_it_never_shows(repo, fake_keyring, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    assert keys.main(["rotate", "--generate"]) == 0
    new = fake_keyring.stored
    assert new != h.PASSPHRASE and len(new) >= 43
    out = capsys.readouterr()
    assert new not in out.out + out.err
    assert keys.check(repo=repo, deep_passphrase=new) == []


# ---------------------------------------------------------------------------
# copy-passphrase
# ---------------------------------------------------------------------------

FAKE_TOOL = (["clip"], "utf-16-le", ["clip"])


@pytest.fixture
def clipboard(monkeypatch):
    runs = []
    monkeypatch.setattr(keys, "clipboard_command", lambda *a, **k: FAKE_TOOL)
    monkeypatch.setattr(keys, "_run_clipboard", lambda argv, data: runs.append((list(argv), data)))
    monkeypatch.setattr(keys.time, "sleep", lambda s: runs.append(("slept", s)))
    return runs


def test_copy_passphrase_fails_loudly_when_nothing_is_stored(fake_keyring, clipboard, capsys):
    assert keys.main(["copy-passphrase"]) == 2
    err = capsys.readouterr().err
    assert "No passphrase in the OS credential store" in err and "nothing was copied" in err
    assert clipboard == []


def test_copy_passphrase_fails_loudly_without_a_clipboard_tool(fake_keyring, monkeypatch, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    monkeypatch.setattr(keys, "clipboard_command", lambda *a, **k: None)
    assert keys.main(["copy-passphrase"]) == 2
    out = capsys.readouterr()
    assert "No clipboard tool" in out.err
    assert h.PASSPHRASE not in out.out + out.err


def test_copy_passphrase_copies_exactly_the_secret_never_prints_it_and_clears_after(fake_keyring, clipboard, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    assert keys.main(["copy-passphrase", "--clear-after", "7"]) == 0
    # UTF-16LE with no BOM and no trailing newline: what Windows clip puts on the clipboard verbatim.
    assert clipboard == [(["clip"], h.PASSPHRASE.encode("utf-16-le")), ("slept", 7), (["clip"], b"")]
    out = capsys.readouterr()
    assert h.PASSPHRASE not in out.out + out.err
    assert "keyring set ai-pc-evals answers-passphrase" in out.out
    assert "never paste it into a chat" in out.out.lower() and "agent" in out.out
    assert "Clipboard cleared" in out.out


def test_copy_passphrase_clear_after_zero_leaves_it(fake_keyring, clipboard):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    assert keys.main(["copy-passphrase", "--clear-after", "0"]) == 0
    assert clipboard == [(["clip"], h.PASSPHRASE.encode("utf-16-le"))]


def test_copy_passphrase_reads_the_keyring_not_the_environment(fake_keyring, clipboard, monkeypatch, capsys):
    monkeypatch.setenv(keys.PASSPHRASE_ENV, h.PASSPHRASE)
    assert keys.main(["copy-passphrase"]) == 2
    assert clipboard == []
    assert h.PASSPHRASE not in capsys.readouterr().err


def test_clipboard_tools_per_platform(monkeypatch):
    monkeypatch.setattr(keys.shutil, "which", lambda name: f"/bin/{name}")
    assert keys.clipboard_command("win32", {}) == (["/bin/clip"], "utf-16-le", ["/bin/clip"])
    assert keys.clipboard_command("darwin", {})[0] == ["/bin/pbcopy"]
    assert keys.clipboard_command("linux", {"WAYLAND_DISPLAY": "w"})[0] == ["/bin/wl-copy"]
    assert keys.clipboard_command("linux", {})[0] == ["/bin/xclip", "-selection", "clipboard"]
    monkeypatch.setattr(keys.shutil, "which", lambda name: None)
    assert keys.clipboard_command("linux", {}) is None
    assert keys.clipboard_command("win32", {}) is None


# ---------------------------------------------------------------------------
# fingerprint: compare two PCs without showing the passphrase
# ---------------------------------------------------------------------------


def _short(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


@pytest.fixture
def no_heldout_env(monkeypatch):
    monkeypatch.delenv(keys.HELDOUT_PASSPHRASE_ENV, raising=False)


def test_fingerprint_prints_length_and_short_hash_never_the_value(fake_keyring, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    assert keys.main(["fingerprint"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines()[0] == (f"answers-passphrase: {len(h.PASSPHRASE)} characters, fingerprint "
                                       f"{_short(h.PASSPHRASE)} (from the credential store, "
                                       "ai-pc-evals/answers-passphrase)")
    assert h.PASSPHRASE not in out.out + out.err
    assert not any(word in out.out for word in h.PASSPHRASE.split() if len(word) > 3)


def test_fingerprint_says_the_environment_wins_and_shows_both(fake_keyring, monkeypatch, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    monkeypatch.setenv(keys.PASSPHRASE_ENV, NEW)
    assert keys.main(["fingerprint"]) == 0
    out = capsys.readouterr().out
    assert f"{len(NEW)} characters, fingerprint {_short(NEW)} (from {keys.PASSPHRASE_ENV} in this shell)" in out
    assert f"credential store: {len(h.PASSPHRASE)} characters, fingerprint {_short(h.PASSPHRASE)}" in out
    assert NEW not in out and h.PASSPHRASE not in out


def test_fingerprint_heldout_reads_the_heldout_passphrase(fake_keyring, no_heldout_env, monkeypatch, capsys):
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.KEYRING_USER, h.PASSPHRASE)
    fake_keyring.set_password(keys.KEYRING_SERVICE, keys.HELDOUT_KEYRING_USER, NEW)
    assert keys.main(["fingerprint", "--heldout"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == (f"heldout-passphrase: {len(NEW)} characters, fingerprint {_short(NEW)} "
                                   "(from the credential store, ai-pc-evals/heldout-passphrase)")
    assert _short(h.PASSPHRASE) not in out and NEW not in out
    monkeypatch.setenv(keys.HELDOUT_PASSPHRASE_ENV, h.PASSPHRASE)
    assert keys.main(["fingerprint", "--heldout"]) == 0
    out = capsys.readouterr().out
    assert f"fingerprint {_short(h.PASSPHRASE)} (from {keys.HELDOUT_PASSPHRASE_ENV} in this shell)" in out
    assert f"credential store: {len(NEW)} characters, fingerprint {_short(NEW)}" in out
    assert h.PASSPHRASE not in out


def test_fingerprint_without_a_passphrase_fails_with_the_usual_fix(fake_keyring, no_heldout_env, repo, capsys):
    assert keys.main(["fingerprint"]) == 2
    out = capsys.readouterr()
    assert "No passphrase for the encrypted answers" in out.err
    assert "keyring set ai-pc-evals answers-passphrase" in out.err and "fingerprint" not in out.out
    assert keys.main(["fingerprint", "--heldout"]) == 2
    assert "keyring set ai-pc-evals heldout-passphrase" in capsys.readouterr().err


def test_fingerprint_once_published_says_no_answers_passphrase_is_needed(fake_keyring, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(keys, "REPO", tmp_path)
    (tmp_path / keys.ANSWERS_DIR).mkdir()
    (tmp_path / keys.ANSWERS_DIR / "tasks.json").write_text("{}", encoding="utf-8")
    assert keys.main(["fingerprint"]) == 0
    assert "none is needed" in capsys.readouterr().out
