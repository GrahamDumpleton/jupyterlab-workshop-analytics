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


def test_an_unknown_field_is_kept_and_named(validator: EventValidator) -> None:
    """A closed object's extra field is reported, never a problem.

    A newer extension may send a field this copy of the schema does not
    list; the event stays valid and the field is named by its path so
    the pipeline can say what it saw.
    """

    fixture = load_fixture("why-a-workshop")
    start = next(e for e in fixture if e["kind"] == "workshop-start")
    event = dict(start)
    event["pages"] = [{**page, "colour": "red"} for page in start["pages"]]

    verdict = validator.check(event)

    assert verdict.problems == []
    assert verdict.unknown == ["pages[].colour"]
    assert validator.problems(event) == []

    # A real problem beside an unknown field is still a problem.
    broken = {**event, "page": 7}
    verdict = validator.check(broken)

    assert verdict.problems and "page" in verdict.problems[0]
    assert verdict.unknown == ["pages[].colour"]


def test_unknown_fields_are_counted_and_the_first_sighting_is_told_apart() -> None:
    validator = EventValidator()

    assert validator.notice("pages[].colour") is True
    assert validator.notice("pages[].colour") is False
    assert validator.notice("tools[].licence") is True
    assert validator.unknown_seen == {"pages[].colour": 2, "tools[].licence": 1}
