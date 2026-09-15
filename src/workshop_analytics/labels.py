"""The rules for labels, shared with the extension.

A label is a key and value pair stamped on every event for slicing
reports. The shape rules here are the extension's, so a block the
extension accepts is a block the service accepts, and a token's labels
are held to the same rules as a manifest's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

LABEL_KEY_PATTERN = re.compile(r"^[a-z0-9_.-]+$")

MAX_LABEL_KEY_LENGTH = 63

MAX_LABEL_VALUE_LENGTH = 128

MAX_LABELS = 16

CLIENT_PREFIX = "client."


def label_problems(labels: Mapping[str, Any]) -> list[str]:
    """The problems with a set of labels as messages; none for a valid set."""

    problems: list[str] = []

    if len(labels) > MAX_LABELS:
        problems.append(f"at most {MAX_LABELS} labels are allowed, not {len(labels)}")

    for key, value in labels.items():
        if not LABEL_KEY_PATTERN.match(key) or len(key) > MAX_LABEL_KEY_LENGTH:
            problems.append(
                f'label key "{key}" must be lower case letters, digits, '
                f"underscore, dot or hyphen, up to {MAX_LABEL_KEY_LENGTH} characters"
            )

        if not isinstance(value, str):
            problems.append(f'label "{key}" must have a string value')
        elif len(value) > MAX_LABEL_VALUE_LENGTH:
            problems.append(
                f'label "{key}" has a value longer than '
                f"{MAX_LABEL_VALUE_LENGTH} characters"
            )

    return problems


def parse_label(text: str) -> tuple[str, str]:
    """Split a `key=value` option into its parts, refusing a bad shape."""

    key, separator, value = text.partition("=")

    if not separator or not key:
        raise ValueError(f'label "{text}" must be written as key=value')

    problems = label_problems({key: value})

    if problems:
        raise ValueError("; ".join(problems))

    return key, value


def merge_labels(
    trusted: Mapping[str, str], reported: Mapping[str, str]
) -> dict[str, str]:
    """Combine a token's labels with an event's own.

    The token's labels are the operator's signed statement and always
    stand. An event label with the same key is kept under the reserved
    `client.` prefix rather than dropped, so nothing a client sent is
    lost and nothing a client sends can impersonate the operator's
    label.
    """

    merged = dict(trusted)

    for key, value in reported.items():
        if key in trusted:
            merged[CLIENT_PREFIX + key] = value
        else:
            merged[key] = value

    return merged
