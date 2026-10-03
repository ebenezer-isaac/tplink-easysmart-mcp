"""Test-only shim: re-export this repo's core under the canonical package name.

The canonical conformance test (``tests/conformance/test_breaker_contract.py``) is
copied verbatim and hashed by ``core/VERSION``; it imports the canonical package
name ``vigi_nvr_mcp``. ``tests/test_core_identity.py`` proves this repo's ``core``
is byte-identical to the canonical manifest, so importing it under that name runs
the exact same code. This package is never installed or shipped (it lives under
``tests/`` and is only placed on ``sys.path`` by ``tests/conftest.py``); it is NOT
a per-repo variant of ``core`` (the identity test forbids that) - just an alias.
"""
