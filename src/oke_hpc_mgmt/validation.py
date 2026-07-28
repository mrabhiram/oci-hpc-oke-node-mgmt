from __future__ import annotations

import re

_POOL_NAME_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?"
)


def normalize_pool_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("Pool name cannot be empty.")
    if not _POOL_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Pool name must be 1-63 characters, contain only letters, numbers, "
            "'.', '_' or '-', and start and end with a letter or number."
        )
    return name
