"""Matching primitives shared by review and document classification."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import islice
from typing import Literal

import re2

Scope = Literal["required", "filename", "body", "structure"]
Format = Literal["literal", "regex"]


@lru_cache(maxsize=512)
def compile_expression(value: str):
    options = re2.Options()
    options.case_sensitive = False
    options.log_errors = False
    return re2.compile(value, options=options)


@dataclass(frozen=True, slots=True)
class Expression:
    value: str
    is_regex: bool
    weight: float = 1.0

    def count(self, text: str) -> int:
        """Count up to three nonempty matches for the classifier's scoring cap."""
        if not self.is_regex:
            return min(text.count(self.value), 3) if self.value else 0
        hits = (hit for hit in compile_expression(self.value).finditer(text) if hit.start() < hit.end())
        return sum(1 for _ in islice(hits, 3))

    def matches(self, text: str) -> bool:
        if not self.is_regex:
            return self.value.casefold() in text.casefold()
        hit = compile_expression(self.value).search(text)
        return hit is not None and (hit.start() < hit.end() or self.count(text) > 0)
