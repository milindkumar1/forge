# Forge: Codex project instructions

Read `PROJECT_BRIEF.md` before major decisions. Forge is a working evaluation/post-training/inference software project; literature notes support learning but are not the product.

## Engineering workflow
- Make small, vertical, tested changes. Start with a usable minimal eval CLI instead of scaffolding an enormous architecture.
- Before each substantial change, explain the intent, files, tradeoffs, and acceptance criteria in a few sentences.
- Implement, run relevant tests, report exactly what passed/failed, and propose the next smallest step.
- Keep code typed, modular, understandable and secure; avoid unnecessary dependencies.
- Do not fabricate research results, benchmark numbers, citations, or measured speedups.
- Never execute generated/untrusted code outside a suitable sandbox. Never commit tokens, credentials, personal files, or `.env`.
- Use fixture/mock providers for offline CI, and make any network inference opt-in.
- Keep training and held-out evaluation tasks separated; preserve provenance and reproducibility.
- Inspect the actual repository before changing files; do not overwrite work without review.

## Learning mode
The owner wants to understand AI research and systems engineering deeply. After a meaningful code change:
1. Summarize the design and explain one interesting technical tradeoff.
2. Ask one short question about the code, algorithm, or architecture and let the owner answer before giving the explanation when interactive.
3. For harder concepts, point to a relevant credible paper or primary documentation, and distinguish established facts from hypotheses.
4. When paired with hyperfocus, avoid duplicating constant quizzes; focus on deeper architecture and math questions.

## First task
Read `PROJECT_BRIEF.md`, propose a minimal implementation plan, then implement Milestone 1: Python CLI, deterministic task/verifier fixtures, mock provider, JSON report, tests, clear README, and optional provider abstraction. Do not start RL or distributed scheduling yet. Review diffs and test output with the owner.

## Git Workflow

- Commit regularly after completing each meaningful,
  self-contained change.
- Use Conventional Commits:
  `type: short description`
- Keep commit messages to one line, concise,
  lowercase, and in the imperative mood.
- Do not add commit bodies or emojis.

### Types
- `feat`: new functionality
- `fix`: bug fixes
- `refactor`: code restructuring
- `perf`: performance improvements
- `test`: adding or updating tests
- `docs`: documentation changes
- `chore`: maintenance and configuration
- `ci`: CI/CD changes

### Commit rules
- Run relevant tests before committing.
- Commit only related files.
- Never commit secrets, `.env` files,
  large model weights, or generated artifacts.
- Never force-push or rewrite history.
- Do not push unless explicitly requested.
- Continue working after each commit.
- Summarize commits and tests at task completion.