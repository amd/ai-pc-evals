# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""TLS for downloads: verify certificates against the operating system's certificate store.

A corporate proxy that inspects TLS (AMD's Zscaler, for one) presents its own root certificate. IT installs
that root in the Windows (or macOS) certificate store, but Python checks downloads against its own bundled
list (certifi), which doesn't have it, so every download fails with ``CERTIFICATE_VERIFY_FAILED``.
:func:`use_os_trust_store` makes Python verify against the OS store instead, with the ``truststore`` package
(MIT). ``REQUESTS_CA_BUNDLE`` still works: a bundle it names is trusted as well.

Grading never depends on this: it only decides whether a download can be made. Standard library only, apart
from the optional ``truststore``.
"""

from __future__ import annotations

import os
import ssl
from typing import Optional

_injected = False

TLS_HINT = (
    "TLS certificate verification failed. Behind a corporate proxy that inspects TLS (Zscaler, for one), "
    "downloads must trust the proxy's root certificate: install the requirements "
    "(`python -m pip install -r requirements.txt -c constraints.txt`), whose truststore package makes Python "
    "use the Windows or macOS certificate store, where IT installs that certificate. If it still fails, export "
    "the proxy's root certificate as a PEM file and set REQUESTS_CA_BUNDLE to its path. See docs/RUNNING.md, "
    "'Corporate proxies'."
)


def use_os_trust_store() -> bool:
    """Make ``ssl``, urllib and requests verify against the OS certificate store; True if that is in effect.

    Idempotent. Without ``truststore`` installed it does nothing and returns False (Python's own list is used).
    """
    global _injected
    if _injected:
        return True
    try:
        import truststore
    except ImportError:
        return False
    truststore.inject_into_ssl()
    _injected = True
    return True


def ca_bundle() -> Optional[str]:
    """The extra CA bundle named by ``REQUESTS_CA_BUNDLE``, for urllib (requests reads it itself)."""
    return os.environ.get("REQUESTS_CA_BUNDLE") or None


def ssl_context() -> Optional[ssl.SSLContext]:
    """A context for urllib that also trusts ``REQUESTS_CA_BUNDLE``, or None for urllib's default."""
    bundle = ca_bundle()
    return ssl.create_default_context(cafile=bundle) if bundle else None


def is_cert_error(exc: BaseException) -> bool:
    """Whether *exc*, or anything it wraps, is a failed certificate check (urllib, requests and urllib3)."""
    seen = set()
    todo: list = [exc]
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            return True
        todo += [x for x in (e.__cause__, e.__context__, getattr(e, "reason", None), *getattr(e, "args", ()))
                 if isinstance(x, BaseException)]
    return False
