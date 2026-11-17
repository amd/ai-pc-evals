# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Downloads on a fresh PC: the OS certificate store (corporate proxies), and the team mirror in the harness.

All offline: urlopen, requests and truststore are replaced."""
import hashlib
import ssl
import sys
import types
import urllib.error

import pytest

import build_fixtures as bf
import snapshot_sources as ss
from kwengine import tls, workspace

URL = "https://example.com/doc.pdf"


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fake_truststore(monkeypatch):
    """A stand-in truststore that records the injection, and a fresh injection state."""
    calls = []
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(inject_into_ssl=lambda: calls.append(1)))
    monkeypatch.setattr(tls, "_injected", False)
    return calls


@pytest.fixture
def no_mirror(monkeypatch):
    monkeypatch.delenv("KW_MIRROR", raising=False)


class _Resp:
    def __init__(self, data=b"x"):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        data, self.data = self.data, b""
        return data


# ---- the OS certificate store -------------------------------------------------------------------------------


def test_truststore_is_injected_once(fake_truststore):
    assert tls.use_os_trust_store() and tls.use_os_trust_store()
    assert fake_truststore == [1]


def test_without_truststore_python_keeps_its_own_list(monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)  # import truststore now raises ImportError
    monkeypatch.setattr(tls, "_injected", False)
    assert tls.use_os_trust_store() is False


def test_the_harness_injects_before_it_downloads(fake_truststore, monkeypatch, tmp_path):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def fake_urlopen(request, timeout):
        assert fake_truststore == [1], "downloaded before the OS store was in use"
        return _Resp()

    monkeypatch.setattr(workspace.urllib.request, "urlopen", fake_urlopen)
    workspace._download(URL, tmp_path / "a")


def test_the_harness_keeps_requests_ca_bundle(fake_truststore, monkeypatch, tmp_path):
    bundle = tmp_path / "proxy-root.pem"
    bundle.write_text("x")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    made, seen = object(), {}

    def fake_context(cafile=None):
        seen["cafile"] = cafile
        return made

    monkeypatch.setattr(tls.ssl, "create_default_context", fake_context)

    def fake_urlopen(request, timeout, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(workspace.urllib.request, "urlopen", fake_urlopen)
    workspace._download(URL, tmp_path / "a")
    assert seen == {"cafile": str(bundle), "context": made}


def test_build_fixtures_injects_before_requests(fake_truststore, monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            return [b"data"]

    def fake_get(url, **kw):
        assert fake_truststore == [1], "downloaded before the OS store was in use"
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    bf._download(URL, tmp_path / "a")
    assert (tmp_path / "a").read_bytes() == b"data"


def _cert_error():
    return ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                           "unable to get local issuer certificate")


def test_cert_errors_are_recognised_however_wrapped():
    requests = pytest.importorskip("requests")
    assert tls.is_cert_error(_cert_error())
    assert tls.is_cert_error(urllib.error.URLError(_cert_error()))
    wrapped = requests.exceptions.SSLError("HTTPSConnectionPool(host='x'): Max retries exceeded "
                                           "(Caused by SSLError(SSLCertVerificationError(1, "
                                           "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')))")
    assert tls.is_cert_error(wrapped)
    assert not tls.is_cert_error(urllib.error.URLError(ConnectionResetError("reset")))
    assert not tls.is_cert_error(TimeoutError("timed out"))


def test_build_fixtures_names_the_fix_for_a_cert_error_without_retrying(fake_truststore, monkeypatch, tmp_path):
    requests = pytest.importorskip("requests")

    def fake_get(url, **kw):
        raise requests.exceptions.SSLError(_cert_error())

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(bf.time, "sleep", lambda s: pytest.fail("retried a certificate error"))
    with pytest.raises(RuntimeError) as e:
        bf._download(URL, tmp_path / "a")
    msg = str(e.value)
    assert "proxy" in msg and "truststore" in msg and "REQUESTS_CA_BUNDLE" in msg


def test_the_harness_names_the_fix_for_a_cert_error(no_mirror, monkeypatch, tmp_path):
    def fail(url, dest):
        raise urllib.error.URLError(_cert_error())

    monkeypatch.setattr(workspace, "_download", fail)
    with pytest.raises(workspace.InputIntegrityError) as e:
        workspace.fetch(URL, sha(b"x"), "doc.pdf", tmp_path / "cache")
    msg = str(e.value)
    assert "proxy" in msg and "truststore" in msg and "REQUESTS_CA_BUNDLE" in msg


def test_snapshot_sources_injects_and_names_the_fix(fake_truststore, monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def fake_urlopen(request, timeout):
        assert fake_truststore == [1]
        raise urllib.error.URLError(_cert_error())

    monkeypatch.setattr(ss.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ss.SnapshotError, match="REQUESTS_CA_BUNDLE"):
        ss.http_get(URL)


# ---- the team mirror in the harness ---------------------------------------------------------------------------


def _mirror(tmp_path, monkeypatch, data, name="doc.pdf", pin=None):
    pin = pin or sha(data)
    mirror = tmp_path / "mirror"
    (mirror / pin).mkdir(parents=True)
    (mirror / pin / name).write_bytes(data)
    monkeypatch.setenv("KW_MIRROR", str(mirror))
    return mirror


def test_the_harness_takes_a_pinned_file_from_the_mirror(tmp_path, monkeypatch):
    data = b"%PDF the pinned original"
    _mirror(tmp_path, monkeypatch, data)
    monkeypatch.setattr(workspace, "_download", lambda u, d: pytest.fail("downloaded although the mirror has it"))
    got = workspace.fetch(URL, sha(data), "doc.pdf", tmp_path / "cache")
    assert got == tmp_path / "cache" / sha(data) / "doc.pdf" and got.read_bytes() == data


def test_the_harness_ignores_a_mirror_copy_with_other_bytes(tmp_path, monkeypatch):
    pin = sha(b"original")
    mirror = _mirror(tmp_path, monkeypatch, b"tampered", pin=pin)
    monkeypatch.setattr(workspace, "_download", lambda u, d: d.write_bytes(b"changed upstream"))
    monkeypatch.setattr(workspace, "build_pin", lambda url: None)
    with pytest.raises(workspace.InputIntegrityError) as e:
        workspace.fetch(URL, pin, "doc.pdf", tmp_path / "cache")
    msg = str(e.value)
    assert "changed upstream" in msg and str(mirror / pin / "doc.pdf") in msg and "--export-mirror" in msg
    assert not [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]


def test_a_changed_upstream_file_suggests_a_mirror_when_none_is_set(no_mirror, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_download", lambda u, d: d.write_bytes(b"changed upstream"))
    monkeypatch.setattr(workspace, "build_pin", lambda url: sha(b"original"))  # task file agrees with PINS
    with pytest.raises(workspace.InputIntegrityError) as e:
        workspace.fetch(URL, sha(b"original"), "doc.pdf", tmp_path / "cache")
    msg = str(e.value)
    assert "Set KW_MIRROR" in msg and "Setting up a team mirror" in msg
    assert str(tmp_path / "cache" / sha(b"original") / "doc.pdf") in msg


def test_a_task_file_that_disagrees_with_pins_does_not_suggest_a_mirror(no_mirror, tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "_download", lambda u, d: d.write_bytes(b"the new upstream file"))
    monkeypatch.setattr(workspace, "build_pin", lambda url: sha(b"the new upstream file"))
    with pytest.raises(workspace.InputIntegrityError) as e:
        workspace.fetch(URL, sha(b"the old file"), "doc.pdf", tmp_path / "cache")
    msg = str(e.value)
    assert "disagrees with PINS" in msg and "#37" in msg and "mirror can't help" in msg
    assert "Set KW_MIRROR" not in msg


def test_build_pin_reads_the_pins_block():
    url, pin = next(iter(bf.PINS.items()))
    assert workspace.build_pin(url) == pin
    assert workspace.build_pin("https://example.invalid/not-pinned") is None


# ---- PINS ----------------------------------------------------------------------------------------------------


def test_every_pin_is_fetched_by_the_build():
    used = {s.url for srcs in bf.SOURCES.values() for s in srcs} | {s.url for s in bf.EXTRA_SOURCES}
    assert sorted(set(bf.PINS) - used) == []


def test_pins_stay_sorted():
    assert list(bf.PINS) == sorted(bf.PINS)


def test_known_changed_sources_are_all_pinned():
    assert set(bf.KNOWN_CHANGED) <= set(bf.PINS)


def test_export_mirror_includes_sources_fetched_outside_sources(tmp_path, monkeypatch):
    src = bf.Source("x.xml", "https://example.invalid/x.xml", "pd")
    monkeypatch.setattr(bf, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(bf, "SOURCES", {})
    monkeypatch.setattr(bf, "EXTRA_SOURCES", [src])
    monkeypatch.setattr(bf, "PINS", {src.url: sha(b"xml")})
    bf.pinned_path(src, sha(b"xml")).parent.mkdir(parents=True)
    bf.pinned_path(src, sha(b"xml")).write_bytes(b"xml")
    assert bf.export_mirror(tmp_path / "mirror") == (1, [])
    assert (tmp_path / "mirror" / sha(b"xml") / "x.xml").read_bytes() == b"xml"
