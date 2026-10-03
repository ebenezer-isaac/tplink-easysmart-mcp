"""Secret-scan tests.

Sample "leaky" strings are assembled at runtime so this file itself never
contains a literal the scanner would flag.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_no_secrets.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scanner = _load("check_no_secrets", SCRIPT)

DOT = "."


def _ip(*octets: int) -> str:
    return DOT.join(str(o) for o in octets)


LEAKS = {
    "private_ipv4_10": f"camera at {_ip(10, 1, 2, 3)}",
    "private_ipv4_172": f"host {_ip(172, 20, 0, 5)}",
    "private_ipv4_192": f"sw {_ip(192, 168, 1, 10)}",
    "mac_colon": "mac " + ":".join(["a4"] * 6),
    "mac_dash": "mac " + "-".join(["0C"] * 6),
    "hex32": "token " + "ab" * 16,
    "stok": "url /st" + "ok=abc123/ds",
    "cipher_field": '{"cipher' + 'text": "QUJDREVG"}',
    "password_assign": "EASYSMART_PASS" + "WORD=hunter2",
}


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_each_leak_pattern_is_detected(name: str) -> None:
    assert scanner.scan_text(LEAKS[name], "sample.txt"), f"{name} was not detected"


@pytest.mark.parametrize(
    "text",
    [
        f"documentation address {_ip(192, 0, 2, 10)}",
        f"public {_ip(8, 8, 8, 8)}",
        f"not private {_ip(172, 32, 0, 1)}",
        f"not private {_ip(172, 15, 0, 1)}",
        "POST https://<sw-host>/st" + "ok=<token>/ds",
        'url = f"/st' + 'ok={token}/ds"',
        "EASYSMART_PASS" + "WORD=<your-switch-password>",
        "EASYSMART_PASS" + "WORD=",
        '{"cipher' + 'text": "<redacted>"}',
        "hex31 " + "a" * 31,
        "hex33 " + "a" * 33,
        # RFC 7042 documentation MAC range is a sanctioned placeholder.
        "mac 00:00:5E:00:53:10",
        "mac 00:00:5e:00:53:ab",
    ],
)
def test_placeholders_and_benign_text_pass(text: str) -> None:
    assert scanner.scan_text(text, "sample.txt") == []


def test_real_mac_outside_doc_range_is_flagged() -> None:
    # Assembled at runtime so this file holds no literal MAC.
    mac = ":".join(f"{b:02x}" for b in (0x00, 0x11, 0x22, 0x33, 0x44, 0x55))
    assert scanner.scan_text(f"mac {mac}", "sample.txt")


def test_allow_marker_suppresses_a_single_line() -> None:
    text = "token " + "ab" * 16 + "  # secret-scan: allow (documented test vector)"
    assert scanner.scan_text(text, "sample.txt") == []


def test_allow_marker_does_not_leak_to_next_line() -> None:
    text = "ok  # secret-scan: allow\ntoken " + "cd" * 16
    findings = scanner.scan_text(text, "sample.txt")
    assert len(findings) == 1
    assert findings[0].line == 2


def test_finding_reports_file_line_and_rule_but_masks_value() -> None:
    secret = "ab" * 16
    findings = scanner.scan_text("x\ny " + secret, "f.txt")
    assert findings[0].path == "f.txt"
    assert findings[0].line == 2
    assert findings[0].rule == "hex32"
    assert secret not in str(findings[0])


def test_forbidden_paths_are_flagged() -> None:
    assert scanner.forbidden_path_findings([".env", "a/b/prod.env", "captures/x.json"])
    assert scanner.forbidden_path_findings([".env.example", "README.md"]) == []


def test_binary_content_is_skipped(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\x00\x01" + ("ab" * 16).encode())
    assert scanner.scan_file(blob, "blob.bin") == []


def test_content_allowlist_is_empty() -> None:
    assert not scanner.CONTENT_ALLOWLIST


def test_repository_contains_no_secrets() -> None:
    findings = scanner.scan_repo(REPO_ROOT)
    assert findings == [], "\n".join(str(f) for f in findings)


def test_stub_scan_detects_markers_and_ellipsis_bodies(tmp_path: Path) -> None:
    gate = _load("gate", REPO_ROOT / "scripts" / "gate.py")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    marker = "TO" + "DO"
    (pkg / "a.py").write_text(
        f"def f():\n    '''doc'''\n    ...\n\n# {marker}: later\n"
        "def g():\n    raise NotImplemented" + "Error\n",
        encoding="utf-8",
    )
    (pkg / "ok.py").write_text("def h():\n    return ...\n", encoding="utf-8")
    problems = gate.scan_stubs(pkg)
    assert len(problems) == 3
    assert all(p.startswith("pkg/a.py") for p in problems)


def test_stub_scan_is_clean_on_package() -> None:
    gate = _load("gate", REPO_ROOT / "scripts" / "gate.py")
    assert gate.scan_stubs(REPO_ROOT / "tplink_easysmart_mcp") == []
