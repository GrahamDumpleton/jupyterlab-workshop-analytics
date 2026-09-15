"""The vendored event schema and the validator built on it."""

from __future__ import annotations

import pytest

from workshop_analytics.schema import EventValidator, load_schema

from .conftest import load_fixture


@pytest.fixture(scope="module")
def validator() -> EventValidator:
    return EventValidator()


@pytest.mark.parametrize(
    "name", ["why-a-workshop", "guided-not-documented", "hello-jupyterlab"]
)
def test_every_fixture_event_validates(validator: EventValidator, name: str) -> None:
    for event in load_fixture(name):
        assert validator.problems(event) == []


def test_the_schema_lists_the_kinds_the_extension_sends(
    validator: EventValidator,
) -> None:
    assert "heartbeat" in validator.kinds
    assert "workshop-resume" in validator.kinds
    assert "seq" in validator.base_fields
    assert "labels" in validator.base_fields


def test_a_missing_base_field_is_a_problem(validator: EventValidator) -> None:
    event = dict(load_fixture("why-a-workshop")[0])

    del event["seq"]

    problems = validator.problems(event)

    assert problems and "seq" in problems[0]


def test_a_kind_field_is_checked_after_the_base(validator: EventValidator) -> None:
    event = next(e for e in load_fixture("why-a-workshop") if e["kind"] == "page-leave")
    broken = {k: v for k, v in event.items() if k != "active_ms"}

    assert validator.problems(event) == []
    assert any("active_ms" in problem for problem in validator.problems(broken))


def test_an_unknown_kind_is_accepted_on_its_base_fields(
    validator: EventValidator,
) -> None:
    event = dict(load_fixture("why-a-workshop")[0], kind="something-new")

    assert validator.problems(event) == []


def test_labels_are_held_to_the_rules(validator: EventValidator) -> None:
    event = dict(load_fixture("why-a-workshop")[0])

    event["labels"] = {f"k{i}": "v" for i in range(17)}
    assert validator.problems(event)

    event["labels"] = {"Bad Key": "v"}
    assert validator.problems(event)

    event["labels"] = {"course": "x" * 129}
    assert validator.problems(event)

    event["labels"] = {"course": "intro-git", "term.year": "2026"}
    assert validator.problems(event) == []


def test_a_non_object_is_a_problem(validator: EventValidator) -> None:
    assert validator.problems(["not", "an", "event"]) == [
        "an event must be a JSON object"
    ]


def test_the_vendored_schema_carries_its_id() -> None:
    schema = load_schema()

    assert schema["$id"].endswith("/events.schema.json")
    assert schema["$schema"].startswith("http://json-schema.org/draft-07/")
