"""Structural guard for the Phase 1 rule: no code path can place a real order.

These checks scan the source tree and the lockfile. They are blunt on
purpose; if one fires on a legitimate change, update the allowlist here
with a comment explaining why the change is still read-only.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "profit_engine"
HTTP_MODULE = SRC / "venues" / "http.py"

SOURCES = sorted(SRC.rglob("*.py"))

FORBIDDEN = {
    "HTTP write methods": re.compile(r"\.(post|put|patch|delete)\(|\"(POST|PUT|PATCH|DELETE)\"", re.I),
    "Kalshi order/portfolio endpoints": re.compile(r"/portfolio|/orders?\b|/order_groups|/rfqs|/api_keys"),
    "Polymarket trading endpoints": re.compile(r"/order\b|/orders\b|relayer|/auth/api-key"),
    "credentials": re.compile(r"api[_-]?key|private[_-]?key|secret|passphrase|KALSHI-ACCESS|POLY_", re.I),
    "wallet/signing libraries": re.compile(r"eth_account|web3|py_clob_client|cryptography|nacl", re.I),
    "venue SDKs": re.compile(r"^\s*(import|from)\s+(kalshi|polymarket)", re.M),
}


def test_sources_found():
    assert HTTP_MODULE in SOURCES


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_no_forbidden_patterns(name):
    pattern = FORBIDDEN[name]
    hits = []
    for path in SOURCES:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not hits, f"{name} found:\n" + "\n".join(hits)


def test_only_http_module_imports_httpx():
    importers = [p for p in SOURCES if re.search(r"^\s*(import|from)\s+httpx", p.read_text(), re.M)]
    assert importers == [HTTP_MODULE]


def test_lockfile_has_no_venue_sdks_or_wallet_libs():
    lock = (ROOT / "uv.lock").read_text()
    names = set(re.findall(r'^name = "([^"]+)"', lock, re.M))
    banned = {n for n in names if re.search(r"kalshi|polymarket|clob|eth-account|web3", n)}
    assert not banned
