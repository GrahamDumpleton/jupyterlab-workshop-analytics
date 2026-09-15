"""The label selector syntax."""

from __future__ import annotations

import pytest

from workshop_analytics.selectors import (
    SelectorError,
    Term,
    matches,
    parse_selector,
)


def test_equality_inequality_and_sets_parse() -> None:
    terms = parse_selector(
        "course=intro-git, term!=2025,cohort in (a, b),host notin (x)"
    )

    assert terms == (
        Term("course", "=", ("intro-git",)),
        Term("term", "!=", ("2025",)),
        Term("cohort", "in", ("a", "b")),
        Term("host", "notin", ("x",)),
    )


def test_matching_follows_each_operator() -> None:
    labels = {"course": "intro-git", "term": "2026", "cohort": "a"}

    assert matches(labels, parse_selector("course=intro-git"))
    assert not matches(labels, parse_selector("course=other"))
    assert matches(labels, parse_selector("term!=2025"))
    assert matches(labels, parse_selector("missing!=x"))
    assert matches(labels, parse_selector("cohort in (a,b)"))
    assert not matches(labels, parse_selector("missing in (a)"))
    assert matches(labels, parse_selector("missing notin (a)"))
    assert not matches(labels, parse_selector("cohort notin (a)"))
    assert matches(labels, ())


@pytest.mark.parametrize("text", ["course", "course==", "=x", "cohort in a", "a b"])
def test_malformed_selectors_are_refused(text: str) -> None:
    with pytest.raises(SelectorError):
        parse_selector(text)
