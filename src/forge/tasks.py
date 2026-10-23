"""Versioned development fixtures. Nothing in this module executes model output."""

import json
from dataclasses import dataclass
from typing import Tuple

SUITE_ID = "text-dev-v1"


@dataclass(frozen=True)
class Task:
    task_id: str
    operation: str
    text: str
    expected: str
    known_wrong: str

    @property
    def prompt(self) -> str:
        return json.dumps({
            "instruction": (
                "Return only the answer, with no commentary. reverse: reverse the text's "
                "Unicode code points; word_count: count whitespace-separated words; "
                "sort_numbers: sort comma-separated integers ascending, retaining duplicates, "
                "and join with commas and no spaces."
            ),
            "operation": self.operation,
            "text": self.text,
        }, ensure_ascii=False, sort_keys=True)

    def verify(self, answer: str) -> bool:
        # Strict output contract: extra whitespace is a failure too.
        return answer == self.expected


TASKS: Tuple[Task, ...] = (
    Task("reverse-basic", "reverse", "forge", "egrof", "forge"),
    Task("reverse-unicode", "reverse", "a\u00e9\u732b", "\u732b\u00e9a", "a\u00e9\u732b"),
    Task("count-whitespace", "word_count", " one\t two\nthree  ", "3", "5"),
    Task("count-empty", "word_count", "", "0", "1"),
    Task("sort-numeric", "sort_numbers", "10,-2,3,3,0", "-2,0,3,3,10", "-2,0,10,3,3"),
    Task("sort-single", "sort_numbers", "5", "5", ""),
)
