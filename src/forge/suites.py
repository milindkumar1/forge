"""Data-only task suites and trusted, provider-independent quality checks."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from forge.tasks import SUITE_ID, TASKS, Task

VALIDATOR_VERSION = "text-validator-v1"
OPERATIONS = ("reverse", "word_count", "sort_numbers")
MAX_SUITE_BYTES = 1_000_000


@dataclass(frozen=True)
class TaskSuite:
    id: str
    split: str
    source: str
    tasks: Tuple[Task, ...]
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id,
                "split": self.split, "source": self.source,
                "tasks": [asdict(task) for task in self.tasks]}

    @property
    def digest(self) -> str:
        # Hash the logical document: file whitespace/key order do not affect identity.
        content = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


DEFAULT_SUITE = TaskSuite(SUITE_ID, "development", "builtin:forge/text-dev-v1", TASKS)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) is not None


def parse_suite(data: Any) -> TaskSuite:
    fields = {"schema_version", "id", "split", "source", "tasks"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("suite must contain exactly schema_version, id, split, source, tasks")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported task suite schema_version")
    if not _valid_id(data["id"]):
        raise ValueError("suite id must be 1-80 letters, digits, dots, underscores, or hyphens")
    if data["split"] != "development":
        raise ValueError("only development suites are supported; training/held-out suites need a separate workflow")
    if not isinstance(data["source"], str) or not data["source"].strip() or len(data["source"]) > 1000:
        raise ValueError("suite source must be a nonempty string of at most 1000 characters")
    items = data["tasks"]
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError("suite must contain 1-1000 tasks")
    task_fields = {"task_id", "operation", "text", "expected", "known_wrong"}
    tasks = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != task_fields:
            raise ValueError("task {} must contain exactly task_id, operation, text, expected, known_wrong".format(index))
        if not _valid_id(item["task_id"]) or item["task_id"] in seen:
            raise ValueError("task {} has an invalid or duplicate task_id".format(index))
        seen.add(item["task_id"])
        if item["operation"] not in OPERATIONS:
            raise ValueError("task {} has an unsupported operation".format(index))
        for name in ("text", "expected", "known_wrong"):
            if not isinstance(item[name], str) or len(item[name]) > 10_000:
                raise ValueError("task {}: {} must be a string of at most 10000 characters".format(index, name))
        tasks.append(Task(**item))
    return TaskSuite(data["id"], data["split"], data["source"], tuple(tasks))


def load_suite(path: Path) -> TaskSuite:
    with path.open("rb") as handle:
        content = handle.read(MAX_SUITE_BYTES + 1)
    if len(content) > MAX_SUITE_BYTES:
        raise ValueError("task suite exceeds 1000000 bytes")
    return parse_suite(json.loads(content.decode("utf-8")))


def reference_answer(task: Task) -> str:
    """Trusted oracles, separate from provider implementations and file-supplied labels."""
    if task.operation == "reverse":
        return "".join(reversed(task.text))
    if task.operation == "word_count":
        return str(len(re.findall(r"\S+", task.text)))
    # Bound numeric inputs and avoid accepting Python-only spellings such as 1_000.
    if re.fullmatch(r"[+-]?[0-9]{1,18}(?:,[+-]?[0-9]{1,18})*", task.text) is None:
        raise ValueError("sort_numbers requires comma-separated signed integers of at most 18 digits, without spaces")
    # Insert values in order, independently of the mock's sorted() implementation.
    ordered = []
    for token in task.text.split(","):
        number = int(token)
        position = 0
        while position < len(ordered) and ordered[position] <= number:
            position += 1
        ordered.insert(position, number)
    return ",".join(str(number) for number in ordered)


@dataclass(frozen=True)
class Mutant:
    id: str
    operation: str
    answer: Callable[[str], str]


# These are reviewed implementations shipped with Forge, never loaded from a suite.
MUTANTS = (
    Mutant("reverse.identity", "reverse", lambda text: text),
    Mutant("reverse.drop_first", "reverse", lambda text: text[1:][::-1]),
    Mutant("word_count.literal_spaces", "word_count", lambda text: str(len(text.split(" ")))),
    Mutant("word_count.off_by_one", "word_count", lambda text: str(len(text.split()) + 1)),
    Mutant("sort_numbers.lexical", "sort_numbers", lambda text: ",".join(sorted(text.split(",")))),
    Mutant("sort_numbers.deduplicate", "sort_numbers",
           lambda text: ",".join(str(number) for number in sorted({int(token) for token in text.split(",")}))),
)


@dataclass(frozen=True)
class MutationCheck:
    mutant_id: str
    killed_by: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {"mutant_id": self.mutant_id, "killed": bool(self.killed_by), "killed_by": list(self.killed_by)}


@dataclass(frozen=True)
class Validation:
    suite: TaskSuite
    errors: Tuple[str, ...]
    mutations: Tuple[MutationCheck, ...]

    @property
    def accepted(self) -> bool:
        return not self.errors and all(check.killed_by for check in self.mutations)

    def require_valid(self) -> None:
        if not self.accepted:
            details = list(self.errors) + ["surviving mutant: " + check.mutant_id
                                            for check in self.mutations if not check.killed_by]
            raise ValueError("suite validation failed: " + "; ".join(details))

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": 1, "validator_version": VALIDATOR_VERSION,
                "suite_id": self.suite.id, "suite_sha256": self.suite.digest,
                "tasks_checked": len(self.suite.tasks), "accepted": self.accepted,
                "errors": list(self.errors), "mutations": [check.to_dict() for check in self.mutations]}


def validate_suite(suite: TaskSuite) -> Validation:
    # Recheck shape even for suites built by Python callers, before any provider call.
    parse_suite(suite.to_dict())
    errors = []
    for task in suite.tasks:
        try:
            reference = reference_answer(task)
        except ValueError as exc:
            errors.append(task.task_id + ": " + str(exc))
            continue
        if task.expected != reference:
            errors.append(task.task_id + ": expected answer disagrees with trusted reference")
        if task.known_wrong == reference:
            errors.append(task.task_id + ": known_wrong is actually correct")
    if errors:
        # Never score mutants against incorrect labels.
        return Validation(suite, tuple(errors), ())
    operations = {task.operation for task in suite.tasks}
    checks = tuple(
        MutationCheck(mutant.id, tuple(task.task_id for task in suite.tasks
                                      if task.operation == mutant.operation and not task.verify(mutant.answer(task.text))))
        for mutant in MUTANTS if mutant.operation in operations
    )
    return Validation(suite, (), checks)
