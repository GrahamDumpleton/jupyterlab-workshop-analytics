"""Signed tokens: the one credential the service understands.

Every token is a JWT signed with the service's one key, HS256, and
everything the service needs to know about a sender is in the claims:
who was given it (`sub`), what it may do (`scope`), the labels the
operator vouches for, the browser origins it may post from, and when
it starts and stops being valid. There is no token file; verification
is the signature plus the claims, with a deny list of `jti` values for
the token that must die before its expiry.
"""

from __future__ import annotations

import base64
import re
import secrets
import time
import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt

from .labels import label_problems

ALGORITHM = "HS256"

SCOPES = ("ingest", "api", "dashboard")

DEFAULT_SCOPES = ("ingest",)

KEY_BYTES = 32

MIN_KEY_BYTES = 16

DURATION_PATTERN = re.compile(r"^(\d+)([dhm])$")

DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60}


class TokenError(Exception):
    """A token or key that cannot be used, with the reason."""


@dataclass(frozen=True)
class Claims:
    """What a verified or inspected token says."""

    jti: str
    name: str
    scopes: tuple[str, ...]
    labels: dict[str, str] = field(default_factory=dict)
    origins: tuple[str, ...] = ()
    issued_at: int = 0
    not_before: int = 0
    expires_at: int = 0

    def as_dict(self) -> dict[str, Any]:
        """The claims as the JSON payload they were signed from."""

        return {
            "jti": self.jti,
            "sub": self.name,
            "scope": list(self.scopes),
            "labels": dict(self.labels),
            "origins": list(self.origins),
            "iat": self.issued_at,
            "nbf": self.not_before,
            "exp": self.expires_at,
        }


def now() -> float:
    """The verifier's clock, as seconds since the epoch.

    A module attribute rather than a direct `time.time()` call so a test
    can hold it still with a value binding.
    """

    return time.time()


def generate_key() -> str:
    """A fresh random signing key, base64 encoded, of the right length."""

    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def decode_key(value: str) -> bytes:
    """The key bytes behind a base64 setting, refusing a short or bad one."""

    text = value.strip()

    if not text:
        raise TokenError("no signing key: set TOKEN_SIGNING_KEY or pass --key-file")

    padded = text + "=" * (-len(text) % 4)

    try:
        key = base64.b64decode(padded, validate=True)
    except ValueError:
        try:
            key = base64.urlsafe_b64decode(padded)
        except ValueError as error:
            raise TokenError("the signing key is not base64") from error

    if len(key) < MIN_KEY_BYTES:
        raise TokenError(
            f"the signing key must decode to at least {MIN_KEY_BYTES} bytes; "
            "make one with: workshop-analytics key generate"
        )

    return key


def read_key(value: str = "", key_file: Path | None = None) -> bytes:
    """The key from an explicit value or a file, whichever was given."""

    if key_file is not None:
        try:
            return decode_key(key_file.read_text(encoding="utf-8"))
        except OSError as error:
            raise TokenError(f"cannot read the key file: {error}") from error

    return decode_key(value)


def parse_expiry(value: str, base: float) -> int:
    """An expiry as seconds since the epoch, from a date or a duration.

    Accepts a date (`2027-01-31`, the end of that day in UTC), an ISO
    8601 timestamp, or a duration from `base` such as `90d`, `12h` or
    `30m`.
    """

    text = value.strip()
    match = DURATION_PATTERN.match(text)

    if match:
        amount, unit = match.groups()

        return int(base) + int(amount) * DURATION_UNITS[unit]

    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None

    if day is not None and len(text) == 10:
        end = datetime.combine(day, datetime.max.time(), tzinfo=UTC)

        return int(end.timestamp())

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise TokenError(
            f'"{value}" is not an expiry: give a date (2027-01-31), a timestamp '
            "or a duration (90d, 12h, 30m)"
        ) from error

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    return int(moment.timestamp())


def issue(
    key: bytes,
    *,
    name: str,
    expires: int,
    labels: Mapping[str, str] | None = None,
    origins: Iterable[str] = (),
    scopes: Iterable[str] = DEFAULT_SCOPES,
    jti: str = "",
    not_before: int | None = None,
) -> tuple[str, Claims]:
    """Sign a token from its parts; returns the token and its claims."""

    scope_list = tuple(dict.fromkeys(scopes))

    if not name.strip():
        raise TokenError("a token needs a name")

    if not scope_list:
        raise TokenError("a token needs at least one scope")

    for scope in scope_list:
        if scope not in SCOPES:
            raise TokenError(
                f'unknown scope "{scope}"; choose from {", ".join(SCOPES)}'
            )

    problems = label_problems(dict(labels or {}))

    if problems:
        raise TokenError("; ".join(problems))

    issued = int(now())
    starts = issued if not_before is None else not_before

    if expires <= starts:
        raise TokenError("the expiry must be after the token starts being valid")

    claims = Claims(
        jti=jti or uuid.uuid4().hex,
        name=name.strip(),
        scopes=scope_list,
        labels=dict(labels or {}),
        origins=tuple(dict.fromkeys(origin.rstrip("/") for origin in origins)),
        issued_at=issued,
        not_before=starts,
        expires_at=expires,
    )

    token = jwt.encode(claims.as_dict(), key, algorithm=ALGORITHM)

    return token, claims


def _claims_from(payload: Mapping[str, Any]) -> Claims:
    scopes = payload.get("scope", [])
    labels = payload.get("labels", {})
    origins = payload.get("origins", [])

    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise TokenError("the token's scope claim is not a list of strings")

    if not isinstance(labels, dict) or label_problems(labels):
        raise TokenError("the token's labels claim breaks the label rules")

    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise TokenError("the token's origins claim is not a list of strings")

    try:
        return Claims(
            jti=str(payload["jti"]),
            name=str(payload.get("sub", "")),
            scopes=tuple(scopes),
            labels={str(k): str(v) for k, v in labels.items()},
            origins=tuple(origins),
            issued_at=int(payload.get("iat", 0)),
            not_before=int(payload.get("nbf", 0)),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TokenError("the token is missing a required claim") from error


def inspect(token: str) -> Claims:
    """Decode a token's claims without verifying its signature."""

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as error:
        raise TokenError(f"not a token: {error}") from error

    return _claims_from(payload)


def verify(
    token: str,
    key: bytes,
    *,
    scope: str | Collection[str],
    denied: Collection[str] = (),
) -> Claims:
    """Verify a token for a scope; raises `TokenError` for any refusal.

    The signature, the validity window read against `now()`, the scope
    and the deny list are all checked, and every failure is the same
    exception so a caller can answer every bad token alike. Several
    scopes may be given, of which the token needs any one.
    """

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            options={
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "require": ["jti", "exp"],
            },
        )
    except jwt.PyJWTError as error:
        raise TokenError(f"the token does not verify: {error}") from error

    claims = _claims_from(payload)
    moment = now()

    if moment < claims.not_before:
        raise TokenError("the token is not valid yet")

    if moment >= claims.expires_at:
        raise TokenError("the token has expired")

    wanted = (scope,) if isinstance(scope, str) else tuple(scope)

    if not any(name in claims.scopes for name in wanted):
        raise TokenError(f'the token does not carry the "{" or ".join(wanted)}" scope')

    if claims.jti in denied:
        raise TokenError("the token has been revoked")

    return claims


class DenyList:
    """The `jti` values refused before their expiry, read from a file.

    One id per line, blank lines and `#` comments ignored. The file is
    re-read whenever its modification time changes, so a revocation
    needs no restart; a missing file denies nothing.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._stamp: float | None = None
        self._ids: frozenset[str] = frozenset()

    def _refresh(self) -> None:
        if self._path is None:
            return

        try:
            stamp = self._path.stat().st_mtime
        except OSError:
            self._stamp, self._ids = None, frozenset()

            return

        if stamp == self._stamp:
            return

        ids = set()

        for line in self._path.read_text(encoding="utf-8").splitlines():
            text = line.split("#", 1)[0].strip()

            if text:
                ids.add(text)

        self._stamp, self._ids = stamp, frozenset(ids)

    def __contains__(self, jti: object) -> bool:
        self._refresh()

        return jti in self._ids

    def __len__(self) -> int:
        self._refresh()

        return len(self._ids)

    def __iter__(self) -> Any:
        self._refresh()

        return iter(self._ids)


def expiry_text(claims: Claims) -> str:
    """The expiry as an ISO 8601 timestamp, for messages and listings."""

    moment = datetime.fromtimestamp(claims.expires_at, tz=UTC)

    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def lifetime(claims: Claims) -> timedelta:
    """How long the token is valid for, from its start to its expiry."""

    return timedelta(seconds=claims.expires_at - claims.not_before)
