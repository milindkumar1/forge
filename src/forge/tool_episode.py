"""A bounded tool episode over trusted in-memory data; no host file access."""

import hashlib
import json
import platform
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

from forge import __version__
from forge.config import Config
from forge.providers import Provider, ProviderError

MAX_STEPS = 3
MAX_RESPONSE_CHARS = 10_000
TASK_ID = "inventory-lookup-v1"


def snapshot() -> Dict[str, Any]:
    # Return fresh data so callers cannot mutate the fixture for later runs.
    return {
        "task_id": TASK_ID, "split": "development", "source": "forge/tool-fixtures-v1",
        "question": "Read inventory.json and return the integer quantity for washers.",
        "files": {"inventory.json": '{"bolts": 12, "washers": 7, "nuts": 19}'},
        "expected": "7", "max_steps": MAX_STEPS,
    }


def prompt_for(trace: List[Dict[str, Any]]) -> str:
    return json.dumps({
        "operation": "tool_episode",
        "instruction": (
            'Return one JSON object per turn: {"tool":"read_fixture","path":"inventory.json"} '
            'or {"answer":"your answer"}. read_fixture reads only the named fixture. '
            'Read it successfully before answering. You have three turns total.'
        ),
        "question": snapshot()["question"], "available_files": ["inventory.json"],
        "trace": trace,
    }, ensure_ascii=False, sort_keys=True)


def transition(raw: str) -> Tuple[str, Optional[str]]:
    """Parse untrusted text as data and dispatch only the single trusted tool."""
    if len(raw) > MAX_RESPONSE_CHARS:
        return "invalid", None
    try:
        action = json.loads(raw)
    except ValueError:
        return "invalid", None
    if not isinstance(action, dict):
        return "invalid", None
    if set(action) == {"answer"} and isinstance(action["answer"], str):
        return "answer", action["answer"]
    if set(action) == {"tool", "path"} and action["tool"] == "read_fixture":
        if action["path"] == "inventory.json":
            return "tool_result", snapshot()["files"]["inventory.json"]
        return "tool_error", "unknown fixture path"
    return "invalid", None


def evaluate_tool(config: Config, provider: Provider) -> Dict[str, Any]:
    trace: List[Dict[str, Any]] = []
    answer: Optional[str] = None
    error: Optional[str] = None
    read_ok = False
    started = datetime.now(timezone.utc).isoformat()
    begin = perf_counter()
    termination = "budget_exhausted"
    for _ in range(MAX_STEPS):
        prompt = prompt_for(trace)
        try:
            raw = provider.complete(prompt)
            if not isinstance(raw, str):
                raise ProviderError("non-text response")
        except ProviderError:
            error, termination = "provider request failed", "provider_error"
            trace.append({"prompt": prompt, "response": None, "event": termination, "value": error})
            break
        event, value = transition(raw)
        # Oversized responses are not copied into artifacts or subsequent prompts.
        trace.append({"prompt": prompt, "response": raw if len(raw) <= MAX_RESPONSE_CHARS else None,
                      "event": event, "value": value})
        if event == "invalid":
            termination = "invalid_action"
            break
        if event == "answer":
            answer, termination = value, "answered"
            break
        read_ok = read_ok or event == "tool_result"
    passed = read_ok and termination == "answered" and answer == snapshot()["expected"]
    task = snapshot()
    digest = hashlib.sha256(json.dumps(task, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "schema_version": 2, "kind": "tool_episode", "forge_version": __version__,
        "started_at": started,
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "config": config.to_dict(), "task": task, "task_sha256": digest,
        "results": [{"task_id": TASK_ID, "answer": answer, "passed": passed, "error": error,
                     "termination": termination, "trace": trace, "duration_seconds": perf_counter() - begin}],
        "summary": {"total": 1, "passed": int(passed), "failed": int(not passed),
                    "errors": int(error is not None), "pass_rate": float(passed)},
    }


def validate_tool_report(report: Dict[str, Any]) -> None:
    """Replay recorded provider responses without invoking inference or host tools."""
    if report.get("kind") != "tool_episode" or report.get("task") != snapshot():
        raise ValueError("unsupported tool episode snapshot")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("invalid tool episode results")
    trace = results[0].get("trace")
    if not isinstance(trace, list) or not 1 <= len(trace) <= MAX_STEPS:
        raise ValueError("invalid tool episode trace")
    for entry in trace:
        if (not isinstance(entry, dict) or set(entry) != {"prompt", "response", "event", "value"}
                or (entry["response"] is not None and not isinstance(entry["response"], str))):
            raise ValueError("invalid tool episode trace entry")

    class ReplayProvider:
        def __init__(self) -> None:
            self.index = 0

        def complete(self, prompt: str) -> str:
            if self.index >= len(trace):
                raise ValueError("incomplete tool episode trace")
            entry = trace[self.index]
            self.index += 1
            if prompt != entry["prompt"]:
                raise ValueError("tool episode prompt does not match trace")
            if entry["event"] == "provider_error":
                raise ProviderError("recorded failure")
            if entry["response"] is None:
                return "x" * (MAX_RESPONSE_CHARS + 1)
            return entry["response"]

    replay = ReplayProvider()
    expected = evaluate_tool(Config(), replay)
    if replay.index != len(trace):
        raise ValueError("tool episode contains events after termination")
    for field in ("task_sha256", "summary"):
        if report.get(field) != expected[field]:
            raise ValueError("tool episode evidence is inconsistent")
    for field in ("task_id", "answer", "passed", "error", "termination", "trace"):
        if (results[0].get(field) != expected["results"][0][field]
                or (field == "passed" and type(results[0].get(field)) is not bool)):
            raise ValueError("tool episode result does not match replay")
