# Local inference and measurement

Forge can evaluate an existing OpenAI-compatible chat server on numeric loopback
(`127.0.0.1` or `::1`), including a vLLM server forwarded through SSH. It does not
launch servers, download weights, or provision GPUs. Install the HTTP extra with
`python -m pip install -e '.[http]'`. Fill every placeholder in
`examples/local-vllm.json` and save a separate local config.

The schema-2 config records the served model ID, checkpoint/tokenizer revisions,
model license, dtype, server version, endpoint, stream flag and output cap.
These are operator declarations, not server attestation. Responses that explicitly
identify a different model are rejected; revision/license/dtype cannot be verified
from the chat response. Only `/v1` loopback URLs without credentials/query/fragment
are accepted. Redirects and environment proxy discovery are disabled. This adapter
does not read or send OpenRouter credentials and currently supports no local auth.

```sh
.venv/bin/forge run --native-async --config local.json --allow-network --output local-results.json
.venv/bin/forge report --input local-results.json
.venv/bin/python benchmarks/local_inference.py --config local.json --allow-network \
  --concurrency 1 2 4 --repeats 3 --output /tmp/forge-local-sweep.json
```

The sweep makes one request per task/sample, with no retries, warmups, RPM/TPM
throttles, or circuit breaker. It rotates concurrency order between repetitions
and opens a new pooled session for each sample. Up to 10,000 planned calls are
accepted; invalid inputs and existing output paths fail before dispatch. No
inference is performed without explicit `--allow-network`, even for loopback.
Provider errors and wrong answers remain visible in the saved samples; a valid
measurement artifact is not a claim that all tasks passed.

Generation fixes temperature/seed at zero and records the output cap. Server
nondeterminism, cache state, cold loading, and other clients can still affect
results. The artifact preserves Python/platform/aiohttp/Forge versions, client
source hashes, declared model/runtime configuration, suite digest, policy, raw
reports, and nearest-rank p50/p95/p99 metrics. `measurement_kind` is `endpoint`;
it does not attest that the endpoint ran a real model or a GPU. Tests use an
explicitly named fixture model. GPU telemetry is null, not invented. Benchmark
artifacts contain embedded evaluation reports; `forge report` reads individual
evaluation reports, not the outer benchmark wrapper.

## Timing and usage semantics

Streaming uses bounded incremental UTF-8/SSE decoding: 1 MB wire limit, 65,536
characters per line/event, at most 10,000 nonempty content deltas. Comments,
role-only events, CR/LF/CRLF, multiline data, and usage-only final events are
handled. A supported finish reason (`stop`/`length`) and `[DONE]` are required.
Premature EOF, malformed data, deadlines and cancellation never return partial
text as success. The deadline covers the entire request/body, without resetting
per event. Partial failed-response usage remains unknown and conservatively
retains its token reservation. Bodies and provider exceptions are sanitized.

First-content latency starts before HTTP connection/pool/header waiting and ends
at the first nonempty content delta parsed by the client. Inter-delta gaps use
the same monotonic clock. Network coalescing and batched deltas mean these are
**not exact TTFT or inter-token latency**. No token counts are inferred by counting
chunks, words, or characters. Usage comes from the provider's prompt/completion/
total counts, checked for consistency. Missing counts produce null output-token
throughput and explicit coverage; empty/single-delta responses have no invented
gap metric. Reported output-token throughput covers completed responses only;
unknown tokens from failed requests are excluded and errors are reported separately.

Local JSON requests use the same usage evidence without stream timing. Native
runtime v2 attempts add optional inference metadata while preserving older
reports. The default local reservation is the largest suite prompt's UTF-8 bytes
plus 64 and the configured output cap; it is an estimate, not exact tokenization.
Explicit admission flags can override this policy and will appear in reports.

## Next hardware experiment

Use a pinned checkpoint and server build on existing authorized hardware. Record
GPU model/count, memory, driver/CUDA, launch settings and workload lengths. Add
server/GPU telemetry and a token-aligned timing path, then compare equivalent
workloads with [vLLM serving](https://docs.vllm.ai/en/latest/serving/online_serving/)
and [vllm bench serve](https://docs.vllm.ai/en/latest/cli/bench/serve/). Control cache
and warmup settings before interpreting throughput/latency. The
[SSE standard](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation)
guides framing; it does not establish token boundaries. No real open-weight or
GPU measurements were performed for this implementation.
