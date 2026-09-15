"""Label selectors, in the familiar syntax.

`course=intro-git,term!=2025,cohort in (a,b),host notin (x)`: terms
separated by commas, each an equality, an inequality, or a set
membership. A selector with no terms matches everything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

TERM_PATTERN = re.compile(
    r"^\s*([a-z0-9_.-]+)\s*(?:(=|!=)\s*([^,()=]*?)|\s+(in|notin)\s*\(([^)]*)\))\s*$"
)


@dataclass(frozen=True)
class Term:
    """One clause of a selector."""

    key: str
    operator: str
    values: tuple[str, ...]

    def matches(self, labels: Mapping[str, str]) -> bool:
        """Whether the labels satisfy this clause."""

        value = labels.get(self.key)

        if self.operator == "=":
            return value == self.values[0]

        if self.operator == "!=":
            return value != self.values[0]

        if self.operator == "in":
            return value is not None and value in self.values

        return value is None or value not in self.values


class SelectorError(ValueError):
    """A selector that cannot be parsed."""


def split_terms(text: str) -> list[str]:
    """Split a selector on the commas outside parentheses."""

    terms: list[str] = []
    depth = 0
    current: list[str] = []

    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)

        if char == "," and depth == 0:
            terms.append("".join(current))
            current = []
        else:
            current.append(char)

    terms.append("".join(current))

    return [term for term in terms if term.strip()]


def parse_selector(text: str) -> tuple[Term, ...]:
    """Parse a selector into its terms, refusing a malformed one."""

    terms: list[Term] = []

    for clause in split_terms(text):
        match = TERM_PATTERN.match(clause)

        if match is None:
            raise SelectorError(f'cannot read the selector clause "{clause.strip()}"')

        key, operator, value, set_operator, members = match.groups()

        if operator:
            terms.append(Term(key, operator, (value.strip(),)))
        else:
            values = tuple(item.strip() for item in members.split(",") if item.strip())

            terms.append(Term(key, set_operator, values))

    return tuple(terms)


def matches(labels: Mapping[str, str], terms: tuple[Term, ...]) -> bool:
    """Whether a set of labels satisfies every term."""

    return all(term.matches(labels) for term in terms)
