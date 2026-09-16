"""The event contract, vendored from the extension.

`events.schema.json` is the schema the extension publishes, copied here
at the version noted in `SCHEMA_VERSION`; `docs/development.md` says
how to refresh it when the extension releases. The schema describes
one event: the base fields every event carries, and under
`definitions/kinds` the extra fields of each kind. Validation is the
two steps the schema's own description asks for: the base fields, then
the kind's definition when the kind is known.

The schema is strict where it describes a closed object, such as a
page entry, and the extension's own tests hold it to that. The service
is not: a field the schema does not know is kept in the stored event
and reported back as unknown rather than counted as a problem, so an
extension released with an additive field never costs an old service
a session's head. The ingest pipeline warns once per field name.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker

SCHEMA_FILE = Path(__file__).with_name("events.schema.json")

SCHEMA_VERSION = "0.2.1"


def load_schema() -> dict[str, Any]:
    """The vendored schema as parsed JSON."""

    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    return schema


@dataclass(frozen=True)
class Verdict:
    """What the schema says of one event.

    `problems` are the reasons it is invalid, none when it is accepted;
    `unknown` names the fields the schema does not know, as paths such
    as `pages[].directives`, which the event keeps.
    """

    problems: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


class EventValidator:
    """Validate events against the vendored schema, base fields then kind."""

    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self.schema: dict[str, Any] = dict(schema or load_schema())
        self.unknown_seen: Counter[str] = Counter()
        self._checker = FormatChecker()
        self._base = Draft7Validator(self.schema, format_checker=self._checker)
        self._kinds: dict[str, Draft7Validator] = {}

        definitions = self.schema.get("definitions", {})

        for kind, spec in definitions.get("kinds", {}).items():
            root = {"definitions": definitions, **spec}

            self._kinds[kind] = Draft7Validator(root, format_checker=self._checker)

    @property
    def kinds(self) -> tuple[str, ...]:
        """The kinds the schema defines extra fields for."""

        return tuple(self._kinds)

    @property
    def base_fields(self) -> tuple[str, ...]:
        """The fields every event carries."""

        return tuple(self.schema.get("properties", {}))

    def check(self, event: Any) -> Verdict:
        """Judge one event: its problems, and the fields the schema does not know.

        The base fields are checked first and a failure there is final;
        the kind's fields follow. A field a closed object does not list
        is never a problem, only reported, so the caller can keep the
        event whole and say what it saw.
        """

        if not isinstance(event, dict):
            return Verdict(problems=["an event must be a JSON object"])

        problems, unknown = _split(self._base.iter_errors(event))

        if problems:
            return Verdict(problems=problems, unknown=unknown)

        kind = str(event.get("kind", ""))
        validator = self._kinds.get(kind)

        if validator is not None:
            problems, more = _split(validator.iter_errors(event))
            unknown.extend(more)

        return Verdict(problems=problems, unknown=sorted(set(unknown)))

    def problems(self, event: Any) -> list[str]:
        """The reasons an event is invalid, as messages; none when valid."""

        return self.check(event).problems

    def notice(self, name: str) -> bool:
        """Count one more event carrying an unknown field; true the first time."""

        self.unknown_seen[name] += 1

        return self.unknown_seen[name] == 1


def _split(errors: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Sort validation errors into problems and the unknown fields they name."""

    problems: list[str] = []
    unknown: list[str] = []

    for error in errors:
        if error.validator == "additionalProperties" and isinstance(
            error.instance, dict
        ):
            known = set(error.schema.get("properties", {}))
            prefix = "".join(
                "[]" if isinstance(part, int) else f".{part}"
                for part in error.absolute_path
            ).lstrip(".")

            for name in sorted(set(error.instance) - known):
                unknown.append(f"{prefix}.{name}" if prefix else name)

            continue

        problems.append(_describe(error))

    return problems, unknown


def _describe(error: Any) -> str:
    where = "/".join(str(part) for part in error.absolute_path) or "$"

    return f"{where}: {error.message}"
