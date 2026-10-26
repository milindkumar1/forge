"""Small, strict JSON configuration; credentials never belong here."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Union
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    provider: str = "mock"
    model: str = "mock-v1"
    mock_mode: str = "correct"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalConfig:
    model: str
    base_url: str
    model_revision: str
    tokenizer_revision: str
    model_license: str
    dtype: str
    server_version: str
    max_tokens: int = 256
    stream: bool = False
    schema_version: int = 2
    provider: str = "local"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


ProviderConfig = Union[Config, LocalConfig]


def validate_local_config(config: LocalConfig) -> None:
    if type(config.schema_version) is not int or config.schema_version != 2 or config.provider != "local":
        raise ValueError("local config requires schema_version 2 and provider local")
    for name in ("model", "model_revision", "tokenizer_revision", "model_license", "dtype", "server_version"):
        value = getattr(config, name)
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError("local " + name + " must be a nonempty string of at most 512 characters")
    if type(config.max_tokens) is not int or not 1 <= config.max_tokens <= 32768 or type(config.stream) is not bool:
        raise ValueError("local max_tokens must be in [1, 32768] and stream must be boolean")
    if not isinstance(config.base_url, str) or len(config.base_url) > 256:
        raise ValueError("invalid local base_url")
    try:
        parsed = urlsplit(config.base_url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname in ("127.0.0.1", "::1")
                 and parsed.path in ("/v1", "/v1/") and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and "@" not in parsed.netloc
                 and "?" not in config.base_url and "#" not in config.base_url
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and not any(char.isspace() or ord(char) < 32 for char in config.base_url))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("local base_url must use a numeric loopback host and /v1, without credentials, query or fragment")


def load_config(path: Path) -> ProviderConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 2:
        required = set(LocalConfig.__dataclass_fields__)
        if set(data) != required:
            raise ValueError("local config must contain exactly: " + ", ".join(sorted(required)))
        config = LocalConfig(**data)
        validate_local_config(config)
        return config
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
