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

## Reach for these before writing your own

`host_repo` was copy-pasted into thirteen modules before anyone noticed. Check this table first; if what you need is close but not exact, widen the shared one rather than forking it.

**Fixtures — `conftest.py`:**

| Fixture | Gives you |
| --- | --- |
| `host_repo` | a throwaway host repo: git identity, one commit on HEAD |
| `isolated_tempdir` | autouse — every test's temp dir is its own, so a workspace never lands in the host's |
| `tmp_path` | pytest's own; the root the two above are built on |

**Helpers — `helpers.py`** (plain functions, callable from a fixture or a test body):

| Helper | Gives you |
| --- | --- |
| `git(repo, *args)` | run git in `repo`, stdout stripped, raises on non-zero |
| `init_host_repo(root)` | what `host_repo` is built from — call it directly only for a *second* repo, or one outside `tmp_path` |
| `sh()` | the POSIX sh on this host (Git Bash on Windows) |
| `OK_OUTCOME` | the Outcome a scripted agent reports when the test doesn't care |

Test modules import helpers as a top-level module — `from helpers import git` — because pytest puts the test file's directory on `sys.path`. `mypy_path` in `pyproject.toml` includes `tests` so the type checker resolves it the same way.

**Keep the tables above current.** `test_docs.py` fails if a shared fixture or helper isn't listed here, so adding one means adding its row — the table is what the next agent reads instead of copy-pasting yours.

**Hoist on the second use, not the third.** A fixture or helper used by one module stays in that module. The moment a second module needs it, move it — to `conftest.py` if it's a fixture, `helpers.py` if it's a plain function. And when you catch yourself about to copy setup out of another test module, that *is* the second use: hoist it instead, in the same commit, rather than leaving a third copy for someone else to find.

## Idiom

- **Test through the public surface.** Import from `waystation`, not from a private module, unless the seam under test *is* internal. A test that reaches into internals breaks on refactors that changed no behaviour — that's the tell.
- **Name the behaviour, not the function.** `test_agent_silence_resets_on_output_lines`, not `test_run_agent_3`. Read the name back as a sentence about the library; if it doesn't say anything a user would care about, the test is probably at the wrong seam.
- **Never wall-sleep to test a bound.** `use_clock(ManualClock())` makes time explicit; `clock.advance(seconds)` wakes the sleepers. A `time.sleep` in a timeout test is a flake waiting for a slow CI box. The same goes for real work a `ManualClock` test waits on before advancing — a commit, a file, a process: poll for the signal itself (`test_timeouts.py`'s `_poll_until`), because a sleep long enough on your box is a guess someone else's box loses.
- **`asyncio_mode = "auto"`**, so an async test needs no decorator. (Plenty of older tests still carry `@pytest.mark.asyncio`; harmless, not required.)
- **`isolated_tempdir` is autouse** — every test gets its own temp dir, so a workspace never lands in the host's. Don't reach for `tempfile.mkdtemp` directly.
- **Assert on log records, not on rendered text.** `caplog.at_level(level, logger="waystation")`, then filter `caplog.records` by `record.name`. The console's formatting is not the contract; the logger a line goes to, and the level it goes at, are.

## Coverage

The floor is 85% and CI enforces it. It's a floor, not a target — don't write a test to move the number. If a branch is genuinely unreachable on this platform, say so where it lives rather than chasing it.
