# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.4] - 2026-08-14

### Fixed

- `tests/test_docs.py` imported PyYAML at module top level, so the PyPI
  publish CI (`uv run --locked pytest`, no `docs` group) failed with
  `ModuleNotFoundError: No module named 'yaml'`. The nav-integrity test now
  uses `pytest.importorskip("yaml")` and skips when the `docs` group is not
  installed; the other docs checks run without PyYAML.

## [1.4.3] - 2026-08-14

### Added

- MkDocs Material documentation site (`docs/`) with Quickstart, Scenarios
  (ingestion, `derive` lineage, autotrace, capture levels, FastAPI, sharing,
  live viewer), and Demos. Built and deployed to GitHub Pages by the updated
  `pages.yml` workflow alongside the interactive demo replays.
- `docs` dependency group (`mkdocs`, `mkdocs-material`, `pymdown-extensions`).
- `tests/test_docs.py` regression suite: nav integrity, required files,
  `derive` provenance-loss content guard, and broken-admonition syntax check.

### Changed

- `pages.yml` now builds the MkDocs site into `_site` and serves demos under
  `/demos/` (previously demos at site root via a hand-written landing page).
- README demo links updated to the new `/demos/` Pages paths.

### Removed

- `pages/index.html` and `tests/test_pages.py` (replaced by the MkDocs site
  root and `tests/test_docs.py`).

## [1.4.2] - 2026-08-13

### Fixed

- `uv.lock` was left out of the 1.4.1 version bump, so CI's `uv run --locked`
  (publish) and `uv sync --locked` (pages) failed with a stale-lockfile error.
  The lockfile is regenerated and committed.

### Notes

- `AGENTS.md` now requires any `pyproject.toml` change (incl. version bumps) to
  run `uv lock` and commit `uv.lock` in the same change, and adds `uv lock --check`
  to the pre-commit gate.

## [1.4.1] - 2026-08-13

### Fixed

- Static HTML render escaped the payload bundle against `</script>` injection
  but inserted graph JSON raw, so a trace name or metadata value containing
  `</script>` broke every artifact. Graph JSON now uses the same escaping.
- Recorder enter now pushes its execution frame as the last caller-visible
  mutation, so an artificial failure mid-enter can no longer strand a frame on
  the context's execution stack.

### Changed

- Internal cleanup (no API change): collapsed triplicated edge serialization to
  a single `Trace._edge_dict`, removed the enter-rollback snapshots in favor of
  fallible-first ordering, merged the duplicated SSE graph/exec emit branches,
  replaced the SSE console dual-buffer with a single array + rAF, deduplicated
  localStorage persistence behind `store` helpers, and removed dead config
  knobs (`preview_limit`, `value_limit`, `server_port`), dead CSS, and the
  vestigial `GET /graph` debug endpoint.
- `Edge.metadata["phase"]` literals are now sourced from `model.PHASE_ARGUMENTS`,
  `PHASE_EXCEPTION`, and `PHASE_RETURN` constants instead of ad-hoc strings.
- `SidecarBackend` now requires an explicit `sidecar_name` (callers always pass
  one).

## [1.4.0] - 2026-08-13

### Added

- `pylier.autotrace()` for process-global, decorator-free tracing of public
  application functions and methods through Python 3.12+ `sys.monitoring`.
- Autotrace controls for application module scope, empty-call filtering,
  Logfire-style execution-time warm-up, and simple-name prefix filtering.
- Single-invocation lifecycle handling for coroutines, generators, and async
  generators, including safe suspension and cross-context resumption.

### Changed

- Explicit `@pylier.node` instrumentation remains authoritative when autotrace
  is active, preventing duplicate nodes and invocations.
- Recorder entry rollback now restores only touched state without copying
  accumulated payload or handoff collections.

### Fixed

- Buffered empty-call traces retain child lineage, invocation references,
  payload bounds, sidecar events, and trace isolation when the empty parent is
  omitted.
- Slow exceptional calls now promote execution-time-filtered functions for
  subsequent capture.

### Notes

- Autotrace covers Python-defined callables in application scope; C callables,
  private/synthetic code, dependencies, and the standard library are skipped.
- For `allow_empty=False`, values equal to declared defaults are treated as
  omitted business inputs. Use `allow_empty=True` when those calls must remain.

## [1.3.2] - 2026-08-13

### Changed

- **Support Python 3.12+.** Dropped the hard 3.14-only pin; pylier now installs
  and runs on Python 3.12, 3.13, and 3.14.
  - `requires-python` relaxed from `>=3.14` to `>=3.12`.
  - Ruff `target-version` lowered from `py314` to `py312`.

### Fixed

- Portability fix in the live viewer's SSE loop: replaced the bare
  `except BrokenPipeError, ConnectionResetError:` (the comma-separated form
  reintroduced in Python 3.14) with the portable parenthesized
  `except (BrokenPipeError, ConnectionResetError):`, which is valid on all
  supported Python versions.

### Notes

- PEP 695 type parameters (`def f[T]`, `def f[**P, R]`) and `typing.override`
  are supported since Python 3.12, so the backward-compatibility work required
  no architecture changes — no reversion to `TypeVar`/`Union`/`Optional`.

### Documentation

- README quick-start: moved the install hint (`# uv add pylier`) inline with
  the code example and repositioned it under the "Quick start" heading.

## [1.3.1] - 2026-08-09

### Added

- Render: trace boundaries are now distinguished from regular algorithm nodes
  in the graph renderer.

## [1.3.0] - 2026-08-09

### Added

- FastAPI integration (`pylier.instrument_fastapi`): pure-ASGI per-request
  tracing middleware, behind the optional `[fastapi]` extra.

## [1.2.0] - 2026-08-09

### Added

- `py.typed` marker for PEP 561 type-information shipping.
- Type-safety: `@pylier.node` preserves the wrapped callable's signature via
  PEP 695 `ParamSpec` overloads; enforced with `ty`.

### Changed

- Reframed README around the live view; added comparison, versioning, and
  roadmap sections.

## [1.1.0] - 2026-08-09

### Added

- `py.typed` marker for PEP 561 type-information shipping.

### Changed

- `@pylier.node` preserves the wrapped callable's signature via PEP 695
  `ParamSpec` overloads; type-safety enforced with `ty`.

## [1.0.0] - 2026-08-09

### Added

- Initial release: decorator-driven data process & pipeline visualization with
  a force-directed graph renderer (single-file HTML, D3.js v7).
- Live viewer (`pylier.serve`) and static render (`pylier.render`).
- In-memory trace, JSONL sidecar sink for offline replay.
- Per-node and global capture levels (`core < info < debug < trace`).
