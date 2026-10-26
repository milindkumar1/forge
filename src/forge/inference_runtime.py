"""Own provider sessions around native evaluations for CLI and benchmark callers."""

from dataclasses import replace
from typing import Any, Dict, Optional

from forge.admission import AdmissionOptions, ProviderCoordinator
from forge.config import LocalConfig, ProviderConfig, validate_local_config
from forge.native_runtime import NativeRuntimeOptions, evaluate_native
from forge.providers import AsyncMockProvider
from forge.suites import TaskSuite, validate_suite


async def run_native(config: ProviderConfig, suite: TaskSuite, options: NativeRuntimeOptions,
                     allow_network: bool, admission: Optional[AdmissionOptions] = None) -> Dict[str, Any]:
    validate_suite(suite).require_valid()
    if isinstance(config, LocalConfig):
        validate_local_config(config)
        admission = admission or AdmissionOptions(failure_threshold=0)
        if admission.tokens_per_request is None:
            reservation = max(len(task.prompt.encode("utf-8")) for task in suite.tasks) + 64 + config.max_tokens
            admission = replace(admission, tokens_per_request=reservation)
    coordinator = ProviderCoordinator(admission) if admission is not None else None
    if config.provider == "mock":
        return await evaluate_native(config, AsyncMockProvider(config.mock_mode), suite, options, coordinator=coordinator)
    try:
        from forge.async_http import AsyncLocalProvider, AsyncOpenRouterProvider
    except ImportError as exc:
        raise ValueError("native HTTP requires: pip install 'forge-evals[http]'") from exc
    settings: Dict[str, Any] = {"allow_network": allow_network,
                                "request_timeout_seconds": options.request_timeout_seconds,
                                "max_connections": options.concurrency}
    provider_instance = (AsyncLocalProvider(config, **settings) if isinstance(config, LocalConfig) else
                         AsyncOpenRouterProvider(config.model, **settings))
    async with provider_instance as provider:
        return await evaluate_native(config, provider, suite, options, coordinator=coordinator)
