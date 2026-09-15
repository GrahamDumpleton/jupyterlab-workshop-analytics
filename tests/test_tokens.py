"""Keys, claims, issuing, verifying and the deny list."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
import wrapture

from workshop_analytics import tokens
from workshop_analytics.tokens import (
    DenyList,
    TokenError,
    decode_key,
    generate_key,
    inspect,
    issue,
    parse_expiry,
    verify,
)

from .conftest import mint


def test_generate_key_is_256_bits_of_base64() -> None:
    key = generate_key()

    assert len(base64.b64decode(key)) == 32
    assert decode_key(key) == base64.b64decode(key)


def test_decode_key_refuses_what_it_cannot_use() -> None:
    with pytest.raises(TokenError, match="no signing key"):
        decode_key("")

    with pytest.raises(TokenError, match="not base64"):
        decode_key("!!!not base64!!!")

    with pytest.raises(TokenError, match="at least 16 bytes"):
        decode_key(base64.b64encode(b"short").decode())


def test_parse_expiry_takes_dates_timestamps_and_durations() -> None:
    base = 1_000_000.0

    assert parse_expiry("90d", base) == 1_000_000 + 90 * 86400
    assert parse_expiry("12h", base) == 1_000_000 + 12 * 3600
    assert parse_expiry("30m", base) == 1_000_000 + 30 * 60
    assert parse_expiry("2027-01-31", base) == 1_801_439_999
    assert parse_expiry("2027-01-31T10:00:00Z", base) == 1_801_389_600

    with pytest.raises(TokenError, match="not an expiry"):
        parse_expiry("soon", base)


def test_issue_refuses_bad_parts(key: bytes) -> None:
    later = int(tokens.now()) + 60

    with pytest.raises(TokenError, match="needs a name"):
        issue(key, name=" ", expires=later)

    with pytest.raises(TokenError, match="unknown scope"):
        issue(key, name="x", expires=later, scopes=("root",))

    with pytest.raises(TokenError, match="after the token starts"):
        issue(key, name="x", expires=int(tokens.now()) - 1)

    with pytest.raises(TokenError, match="label key"):
        issue(key, name="x", expires=later, labels={"Bad": "v"})


def test_issue_and_verify_round_trip(key: bytes) -> None:
    token, claims = mint(
        key, labels={"course": "intro"}, origins=("https://lite.example/",)
    )
    verified = verify(token, key, scope="ingest")

    assert verified == claims
    assert verified.scopes == ("ingest",)
    assert verified.labels == {"course": "intro"}
    assert verified.origins == ("https://lite.example",)
    assert len(verified.jti) == 32


def test_inspect_needs_no_key(key: bytes) -> None:
    token, claims = mint(key, ("api", "dashboard"), name="analyst")

    assert inspect(token) == claims

    with pytest.raises(TokenError, match="not a token"):
        inspect("nope")


def test_verify_refuses_every_bad_token_alike(key: bytes, tmp_path: Path) -> None:
    other = decode_key(generate_key())
    token, claims = mint(key)

    with pytest.raises(TokenError, match="does not verify"):
        verify(token, other, scope="ingest")

    with pytest.raises(TokenError, match="does not verify"):
        verify(token[:-4] + "abcd", key, scope="ingest")

    with pytest.raises(TokenError, match="not a token|does not verify"):
        verify("garbage", key, scope="ingest")

    with pytest.raises(TokenError, match="scope"):
        verify(token, key, scope="api")

    denied = tmp_path / "denied.txt"
    denied.write_text(f"# revoked\n{claims.jti}\n")

    with pytest.raises(TokenError, match="revoked"):
        verify(token, key, scope="ingest", denied=DenyList(denied))


def test_expiry_and_not_before_follow_the_verifier_clock(key: bytes) -> None:
    token, claims = mint(key, expires="1h")
    frozen = wrapture.binding(tokens, attr="now")

    with frozen.overrides(lambda: float(claims.expires_at + 1)):
        with pytest.raises(TokenError, match="expired"):
            verify(token, key, scope="ingest")

    with frozen.overrides(lambda: float(claims.not_before - 1)):
        with pytest.raises(TokenError, match="not valid yet"):
            verify(token, key, scope="ingest")

    with frozen.overrides(lambda: float(claims.not_before + 1)):
        assert verify(token, key, scope="ingest").jti == claims.jti


def test_the_deny_list_reloads_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "denied.txt"
    denied = DenyList(path)

    assert "abc" not in denied

    path.write_text("abc\n")
    os.utime(path, (1_000_000, 1_000_000))

    assert "abc" in denied

    path.write_text("")
    os.utime(path, (1_000_001, 1_000_001))

    assert "abc" not in denied
    assert len(DenyList(None)) == 0


def test_verify_accepts_any_of_several_scopes(key: bytes) -> None:
    token, _ = mint(key, ("dashboard",))

    assert verify(token, key, scope=("api", "dashboard")).scopes == ("dashboard",)

    with pytest.raises(TokenError, match='"api or ingest" scope'):
        verify(token, key, scope=("api", "ingest"))
