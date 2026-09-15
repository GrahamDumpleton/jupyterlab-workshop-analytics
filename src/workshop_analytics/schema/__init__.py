"""The event contract, vendored from the extension.

`events.schema.json` is the schema the extension publishes, copied here
at the version noted in `SCHEMA_VERSION`; `docs/development.md` says
how to refresh it when the extension releases. The schema describes
one event: the base fields every event carries, and under
`definitions/kinds` the extra fields of each kind. Validation is the
two steps the schema's own description asks for: the base fields, then
the kind's definition when the kind is known.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker

SCHEMA_FILE = Path(__file__).with_name("events.schema.json")

SCHEMA_VERSION = "0.2.0"


def load_schema() -> dict[str, Any]:
    """The vendored schema as parsed JSON."""

    schema: dict[str, Any] = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    return schema


class EventValidator:
    """Validate events against the vendored schema, base fields then kind."""

    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self.schema: dict[str, Any] = dict(schema or load_schema())
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

    def problems(self, event: Any) -> list[str]:
        """The reasons an event is invalid, as messages; none when valid."""

        if not isinstance(event, dict):
            return ["an event must be a JSON object"]

        problems = [_describe(error) for error in self._base.iter_errors(event)]

        if problems:
            return problems

        kind = str(event.get("kind", ""))
        validator = self._kinds.get(kind)

        if validator is not None:
            problems = [_describe(error) for error in validator.iter_errors(event)]

        return problems


def _describe(error: Any) -> str:
    where = "/".join(str(part) for part in error.absolute_path) or "$"

    return f"{where}: {error.message}"
