# Tests

Tests are the executable half of the spec. When a ticket settles something structural, leave a test holding it — that survives a refactor in a way a paragraph doesn't.

## Tiers

Mark by what a test needs, so anyone can run the cheap ones anywhere:

| Marker | Needs | Notes |
| --- | --- | --- |
| `unit` | nothing | pure logic; no git, no Docker, no network |
| `git` | a real `git` binary and temp repos | **anything that shells out to git** |
| `docker` | a reachable Docker daemon | `conftest.py` skips it when there isn't one |
| `live` | real agent credentials and money | never selected by default or in CI |

`--strict-markers` is on, so a typo fails collection rather than silently matching nothing. `addopts` carries `-m "not live"`.

## Idiom

- **Test through the public surface.** Import from `waystation`, not from a private module, unless the seam under test *is* internal. A test that reaches into internals breaks on refactors that changed no behaviour — that's the tell.
- **Name the behaviour, not the function.** `test_agent_silence_resets_on_output_lines`, not `test_run_agent_3`. Read the name back as a sentence about the library; if it doesn't say anything a user would care about, the test is probably at the wrong seam.
- **Never wall-sleep to test a bound.** `use_clock(ManualClock())` makes time explicit; `clock.advance(seconds)` wakes the sleepers. A `time.sleep` in a timeout test is a flake waiting for a slow CI box.
- **`asyncio_mode = "auto"`**, so an async test needs no decorator. (Plenty of older tests still carry `@pytest.mark.asyncio`; harmless, not required.)
- **`isolated_tempdir` is autouse** — every test gets its own temp dir, so a workspace never lands in the host's. Don't reach for `tempfile.mkdtemp` directly.
- **Assert on log records, not on rendered text.** `caplog.at_level(level, logger="waystation")`, then filter `caplog.records` by `record.name`. The console's formatting is not the contract; the logger a line goes to, and the level it goes at, are.

## Coverage

The floor is 85% and CI enforces it. It's a floor, not a target — don't write a test to move the number. If a branch is genuinely unreachable on this platform, say so where it lives rather than chasing it.
