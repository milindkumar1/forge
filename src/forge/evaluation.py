"""Sequential evaluation and the versioned report contract."""

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

from forge import __version__
from forge.config import Config
from forge.providers import Provider, ProviderError
from forge.tasks import SUITE_ID, TASKS, suite_digest, validate_fixtures


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(result["passed"] for result in results)
    errors = sum(result["error"] is not None for result in results)
    return {"total": total, "passed": passed, "failed": total - passed,
            "errors": errors, "pass_rate": passed / total if total else 0.0}


def evaluate(config: Config, provider: Provider) -> Dict[str, Any]:
    validate_fixtures()
    results: List[Dict[str, Any]] = []
    started = datetime.now(timezone.utc).isoformat()
    for task in TASKS:
        begin = perf_counter()
        answer: Optional[str] = None
        error: Optional[str] = None
        try:
            answer = provider.complete(task.prompt)
            if not isinstance(answer, str):
                raise ProviderError("provider returned a non-text answer")
        except ProviderError:
            # Custom provider exceptions may carry credentials; keep them out of artifacts.
            answer, error = None, "provider request failed"
        results.append({
            "task_id": task.task_id, "prompt": task.prompt, "expected": task.expected,
            "answer": answer, "passed": error is None and answer is not None and task.verify(answer),
            "error": error, "duration_seconds": perf_counter() - begin,
        })
    return {
        "schema_version": 1,
        "forge_version": __version__,
        "started_at": started,
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "config": config.to_dict(),
        "suite": {"id": SUITE_ID, "split": "development", "sha256": suite_digest()},
        "results": results,
        "summary": summarize(results),
    }


def load_report(path: Path) -> Dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("unsupported report schema_version")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("report must have nonempty results")
    seen = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("invalid report result")
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise ValueError("invalid or duplicate report task_id")
        seen.add(task_id)
        if type(result.get("passed")) is not bool or "error" not in result:
            raise ValueError("invalid report result status")
        if result["error"] is not None and (not isinstance(result["error"], str) or result["passed"]):
            raise ValueError("invalid report error status")
    if report.get("summary") != summarize(results):
        raise ValueError("report summary does not match results")
    return report
