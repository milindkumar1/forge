"""Small, strict JSON configuration; credentials never belong here."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    provider: str = "mock"
    model: str = "mock-v1"
    mock_mode: str = "correct"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != set(Config().to_dict()):
        raise ValueError("config must contain exactly schema_version, provider, model, mock_mode")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported config schema_version")
    if data["provider"] not in ("mock", "openrouter"):
        raise ValueError("provider must be mock or openrouter")
    if not isinstance(data["model"], str) or not data["model"].strip():
        raise ValueError("model must be a nonempty string")
    if data["mock_mode"] not in ("correct", "faulty"):
        raise ValueError("mock_mode must be correct or faulty")
    if data["provider"] == "mock" and data["model"] != "mock-v1":
        raise ValueError("mock provider requires model mock-v1")
    return Config(**data)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    """Create a new file exclusively, preserving existing configs and evidence."""
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
