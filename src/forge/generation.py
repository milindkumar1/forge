"""Seeded development task data from bounded, trusted templates."""

import platform
import random
from typing import List, Set, Tuple
from urllib.parse import urlencode

from forge.suites import TaskSuite, validate_suite
from forge.tasks import Task

GENERATOR_ID = "text-templates-v1"
MAX_ATTEMPTS_PER_TASK = 100
ALPHABET = "bcdefghijk\u00e9\u732b\U0001f408"
WORDS = ("forge", "agent", "eval", "model", "tool", "trace", "input", "answer")


def _candidate(rng: random.Random, index: int) -> Task:
    task_id = "generated-{:04d}".format(index)
    # Include boundary cases once, after three cases that exercise all operations.
    if index == 3:
        return Task(task_id, "reverse", "", "", "nonempty")
    if index == 4:
        return Task(task_id, "word_count", "", "0", "1")
    if index == 5:
        number = str(rng.randint(-20, 20))
        return Task(task_id, "sort_numbers", number, number, "")
    operation = index % 3
    if operation == 0:
        # Different endpoints ensure this is not a palindrome.
        text = "a" + "".join(rng.choice(ALPHABET) for _ in range(rng.randint(2, 12))) + "z"
        return Task(task_id, "reverse", text, text[::-1], text)
    if operation == 1:
        words = [rng.choice(WORDS) for _ in range(rng.randint(2, 6))]
        separator = rng.choice(("\t", "\n", "\u2003", "  "))
        text = "\t" + separator.join(words) + "\n"
        return Task(task_id, "word_count", text, str(len(words)), str(len(words) + 1))
    # A two-digit number beginning with 1, smaller positive duplicates, zero,
    # and a negative number exercise numeric ordering and duplicate retention.
    small = rng.randint(2, 9)
    numbers = [rng.randint(10, 19), small, small, 0, -rng.randint(1, 9)]
    rng.shuffle(numbers)
    text = ",".join(str(number) for number in numbers)
    expected = ",".join(str(number) for number in sorted(numbers))
    known_wrong = ",".join(str(number) for number in sorted(set(numbers)))
    return Task(task_id, "sort_numbers", text, expected, known_wrong)


def generate_suite(*, seed: int = 0, count: int = 12) -> TaskSuite:
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer between 0 and 4294967295")
    if type(count) is not int or not 3 <= count <= 1000:
        raise ValueError("count must be an integer between 3 and 1000")
    # Never seed or consume the process-global random stream.
    rng = random.Random(seed)
    tasks: List[Task] = []
    seen: Set[Tuple[str, str]] = set()
    for index in range(count):
        for _ in range(MAX_ATTEMPTS_PER_TASK):
            task = _candidate(rng, index)
            key = (task.operation, task.text)
            if key not in seen:
                seen.add(key)
                tasks.append(task)
                break
        else:
            raise ValueError("could not generate a unique task within {} attempts".format(MAX_ATTEMPTS_PER_TASK))
    recipe = urlencode({"seed": seed, "count": count, "python": platform.python_version(),
                        "runtime": platform.python_implementation()})
    suite = TaskSuite(
        id="generated-text-v1-s{}-n{}".format(seed, count),
        split="development",
        source="generator:forge/" + GENERATOR_ID + "?" + recipe,
        tasks=tuple(tasks),
    )
    # Reuse the existing gate; never retry a weak suite or loosen the mutation policy.
    validate_suite(suite).require_valid()
    return suite
