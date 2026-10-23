# Forge — Project Brief

## Vision
Forge is an open-source, software-first system for automatically generating and validating evaluations for open-weight coding/tool-use agents, collecting verifiable trajectories, training models on informative failures, and measuring capability improvement per unit of inference compute.

The **software is the product**. Literature notes are private/public learning aids, not the primary deliverable.

## Research hypothesis
Can verified, failure-targeted tasks and quality-aware rollout allocation improve held-out agent performance per GPU-hour versus random sampling and fixed scheduling?

This is a hypothesis, not an established novel contribution. Compare against relevant existing work and avoid unsupported novelty claims.

## Product stages
1. **Usable eval CLI**: `forge init`, `forge generate`, `forge run`, `forge validate`, `forge report`. Start with a small, executable tool-use/coding benchmark and a mock provider; optionally support OpenRouter. Validate generated tests with known mutations.
2. **Trajectory and training pipeline**: capture complete rollout provenance, task/reward checks, model versions and token/cost data. Integrate LoRA/SFT then optionally GRPO on downloadable, trainable open-weight models.
3. **Inference runtime**: local vLLM backend, concurrent rollouts, metrics, baselines and robust retries. Build C++/Rust systems components only when warranted by profiling or a well-defined systems experiment.
4. **Research experiment**: compare random vs failure-targeted task selection and naive vs cost/quality-aware scheduling, on independent held-out tasks with fixed compute budgets.

## Target audience
Developers experimenting with agent evaluations and post-training; xAI-style RL/post-training and Etched-style inference/distributed-systems engineering teams.

## Design commitments
- Provider-neutral inference abstraction: mock + OpenRouter + OpenAI-compatible local endpoints; no claim that arbitrary consumer subscriptions supply API tokens.
- Train only locally accessible and appropriately licensed open-weight checkpoints; OpenRouter is an inference gateway, not a weight-training service.
- Strong data provenance and reproducibility: config, seeds, model IDs/versions, prompts, tool traces, environment snapshots, rewards, timing, token use.
- Isolated execution for untrusted generated code; avoid running arbitrary code directly on the developer host.
- Independent, immutable held-out evaluations. Detect invalid tests and reward hacking.
- Keep API keys in environment variables and never commit secrets.
- Favor measured correctness and readable code over framework complexity.

## Suggested repository
```
forge/
  AGENTS.md
  PROJECT_BRIEF.md
  README.md
  pyproject.toml
  src/forge/{cli.py,config.py,providers/,tasks/,evals/,trajectories/,training/,scheduler/}
  tests/
  examples/
  experiments/
  docs/{architecture.md,research-notes/,decisions/}
  website/
  .github/workflows/
```

## First milestone / acceptance criteria
Create a Python installable CLI and test suite. `forge init` initializes a sample config; `forge run` executes a deterministic mock agent against a small set of verifiable tasks; `forge report` prints and exports a useful JSON report. Add a provider protocol and a *gated optional* OpenRouter adapter, with a mocked test so no paid tokens are needed in CI. Add a simple generated-test validity/mutation check, and document realistic limitations. Do not start with distributed RL or complex UI.

## Site/deployment
Maintain one documentation/demo site under `website/`, built as a static site. Deploy to GitHub Pages at `forge.milindkumar.dev` using a CNAME when DNS ownership and provider are confirmed. Link it from the existing `milindkumar.dev` portfolio. If the existing portfolio permits integration, optionally add `/projects/forge` as a project summary linking to the full Forge site. No hosting of GPU inference workloads on GitHub Pages.

## Initial literature to learn from
- Jimenez et al., *SWE-bench* (2024): https://arxiv.org/abs/2310.06770
- Anthropic, *Bloom* (2025): https://alignment.anthropic.com/2025/bloom-auto-evals/
- Hu et al., *LoRA* (2021): https://arxiv.org/abs/2106.09685
- Rafailov et al., *DPO* (2023): https://arxiv.org/abs/2305.18290
- Shao et al., *DeepSeekMath / GRPO* (2024): https://arxiv.org/abs/2402.03300
- Kwon et al., *PagedAttention / vLLM* (2023): https://arxiv.org/abs/2309.06180
- Zheng et al., *SGLang* (2024): https://arxiv.org/abs/2312.07104
- Sheng et al., *HybridFlow* (2024): https://arxiv.org/abs/2409.19256

For each paper, record its core idea, limitations, the implementation it motivates, and any reproducible experiment. Literature review is supporting documentation only.
