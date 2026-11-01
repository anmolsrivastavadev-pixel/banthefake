# Contributing to BanTheFake

Thank you for helping improve BanTheFake. This project handles a sensitive subject, so accuracy, privacy, and
care for the people affected by deceptive media are part of every contribution.

## Set up a development environment

BanTheFake requires Python 3.12 or newer and uses [uv](https://docs.astral.sh/uv/) for dependencies and commands.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

The core install includes metadata, marker, and C2PA checks. To work on the optional ML detector, install its
extra:

```bash
uv sync --extra ml
```

The two pinned detector models total about 840 MB and are downloaded from Hugging Face on first use. Hugging
Face uses `~/.cache/huggingface` by default. Set `HF_HOME` to a writable location with enough space if you want
the cache elsewhere or want several checkouts to share it:

```bash
export HF_HOME="$HOME/.cache/huggingface"
```

Once the weights are cached, set `HF_HUB_OFFLINE=1` to prevent network access. Do not set that variable before
the first download unless the complete pinned model snapshots are already in `HF_HOME`.

## Run tests

Run the core suite without detector or browser tests:

```bash
uv sync
uv run pytest -q -m "not ml and not e2e"
```

Run the ML tests after installing the extra. The first online run downloads the pinned weights into `HF_HOME`:

```bash
uv sync --extra ml
uv run pytest -q -m ml
```

Run the end-to-end browser tests with the separate dependency group and Chromium:

```bash
uv sync --group e2e
uv run --group e2e playwright install chromium
uv run --group e2e pytest -m e2e
```

On Linux, Playwright may also need system packages; use `playwright install --with-deps chromium` where you can
install them.

## Coding conventions

Follow the patterns in the code you are changing:

- Use modern Python type hints, `pathlib.Path` for paths, small focused functions, and descriptive names.
- Keep standard-library, third-party, and local imports in separate groups.
- Explain security, privacy, or detector-policy decisions with comments that record *why* the constraint exists.
- Add focused pytest coverage for behavior changes and regression fixes. Prefer observable behavior over tests
  that duplicate an implementation.
- Treat uploaded bytes as hostile. Keep parsing and decoding inside the isolation boundary described in
  `docs/ISOLATION-DESIGN.md`, preserve resource bounds, and delete temporary uploads on every outcome.
- Keep the frontend dependency-free and compatible with the strict Content Security Policy. Put JavaScript in
  `static/app.js` and CSS in `static/styles.css`; do not add inline event handlers.
- Update the README or relevant file in `docs/` when behavior, configuration, limits, or measured results change.

The repository does not declare an automatic formatter or linter. Match the surrounding formatting and keep
changes narrow enough to review.

## Honesty and research rules

These rules apply to code, tests, documentation, issues, pull requests, demos, and research:

- Never claim that a file or depicted scene is real. Camera metadata, a low detector score, and an `unknown`
  verdict do not establish authenticity. Even verified C2PA capture provenance cannot prove that a scene was
  unstaged or that a camera did not photograph a screen.
- Report negative, inconclusive, and contradictory results. Do not hide misses, false positives, failed
  experiments, small samples, uncertainty, or known limitations.
- Never train on evaluation data or on fresh confirmation sets. Keep training, calibration/model-selection,
  and final confirmation data separate. A confirmation set stops being fresh after it informs a decision.
- Never create or test with manipulated media of a real identifiable person. Use synthetic faces for
  manipulated-media experiments. Respect fixture licences and record sources and transformations.

Large downloads, experimental scripts, raw outputs, and local research datasets belong under `research/`.
That directory is git-ignored. Summarize methods, reproducible commands, licences, results, and limitations in
the relevant tracked document under `docs/`; do not commit private or unlicensed research data.

## Pull requests

Open a focused pull request that explains the problem, the resulting behavior, and how you validated it. Link
the relevant issue when there is one. Complete the pull request checklist, include documentation with behavior
or configuration changes, and call out limitations or results that did not support the change.

Every GitHub Actions job in `.github/workflows/ci.yml` must pass before merge: core tests, end-to-end tests, ML
tests, and the Docker build and smoke test. Maintainers may ask for a smaller change, more evidence, or changes
that preserve the project's honesty, safety, and privacy rules.
