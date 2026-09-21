# Tests

Tests are the executable half of the spec. When a ticket settles something structural, leave a test holding it — that survives a refactor in a way a paragraph doesn't.

## Tiers

Mark by what a test needs, so anyone can run the cheap ones anywhere:

| Marker | Needs | Notes |
| --- | --- | --- |
| `unit` | nothing | pure logic; no git, no Docker, no network |
| `git` | a real `git` binary and temp repos | **anything that shells out to git** |
| `docker` | a Linux Docker daemon and the `waystation-test` image | `conftest.py` skips it without such a daemon; a missing image fails, saying to run `just test-image` |
| `live` | real agent credentials and money | never selected by default or in CI |

`--strict-markers` is on, so a typo fails collection rather than silently matching nothing. `addopts` carries `-m "not live"`.

## Reach for these before writing your own

`host_repo` was copy-pasted into thirteen modules before anyone noticed. Check this table first; if what you need is close but not exact, widen the shared one rather than forking it.

**Fixtures — `conftest.py`:**

| Fixture | Gives you |
| --- | --- |
| `host_repo` | a throwaway host repo: git identity, one commit on HEAD |
| `image` | the docker tier's image, proved ready — request it in any `docker`-marked test instead of naming `TEST_IMAGE` yourself; it fails with `just test-image` in the message when the image is absent, and asks docker twice when the first lookup could not settle |
| `clean_logging` | the `waystation` loggers put back afterwards: the package logger's handlers and `propagate`, and every level in the subtree — request it in any test that calls `configure_logging`, tunes a logger, or opens a `RunLogFiles` |
| `isolated_tempdir` | autouse — every test's temp dir is its own, so a workspace never lands in the host's |
| `tmp_path` | pytest's own; the root the two above are built on |

**Helpers — `helpers.py`** (plain functions, callable from a fixture or a test body):

| Helper | Gives you |
| --- | --- |
| `git(repo, *args)` | run git in `repo`, stdout stripped, raises on non-zero |
| `git_bytes(repo, *args)` | the same, but stdout exactly as git wrote it — for a test about bytes, where decoding would hide the bug |
| `commit_on(repo, branch, files)` | a commit on `branch` (made at HEAD if missing) with the checkout put back — a target that moved, or a range for `PatchSeries.from_range`; returns the tip |
| `host_state(repo)` | the host's refs, HEAD, index and tree in one value — compare before and after to prove something left the host untouched |
| `init_host_repo(root)` | what `host_repo` is built from — call it directly only for a *second* repo, or one outside `tmp_path` |
| `TEST_IMAGE` | the name of the docker tier's image — for a test that must name it outside the `image` fixture, as a preflight test does |
| `branches(repo, pattern)` | the short names of the branches matching `pattern` — `"waystation/*"` for the preservation branches a run kept |
| `printf_bytes(path, data)` | a sh command writing `data` to `path` byte for byte, as octal escapes — so a CR or a non-UTF-8 byte reaches the file, not just the command line |
| `subjects(repo, revisions)` | the commit subjects in a range like `HEAD..waystation/<id>`, newest first — what a preserved or landed series holds |
| `lifecycle(caplog)` | the records a run logged per lifecycle event (`waystation.run`), in order |
| `workspaces(temp)` | the run workspaces left under a temp dir — assert `== []` to prove a run cleaned up |
| `until(ready, task)` | wait until `ready()` holds — a run reached a known point, say a stalled git — failing at once if the run ends first |
| `stalling_ref_hook(hooks, started, release)` | a `reference-transaction` hook that holds the first ref update git prepares until `release` exists; point a host at it with `core.hooksPath` |
| `awaited(spec)` | a coroutine awaiting a `RunSpec`, for `asyncio.create_task` — reach for it when a test cancels or drives a run from outside |
| `Gate` / `GatedSandbox(gate, at)` | a point a run is held at until the test sets `gate.release` — `gate.reached` once it waits, `gate.passed` once it goes on; `GatedSandbox` is `NoSandbox` held at `"start"`, `"collect"` (its git execs) or `"teardown"`, for cancelling a run mid-stage |
| `a_run(repo)` | a `RunSpec` that says one line, makes one commit, reports an Outcome — chain `.integrate(…)` / `.on_*(…)` onto it; `commits=` swaps in your own series and `sandbox=` your own backend. It runs the same on every backend: the sandbox says which shell (ADR-0036) |
| `ShellAgent(script)` | an agent that *is* a shell script — reach for it over `ScriptedAgent` when the test drives stderr, an exit code, timing, or bytes only `printf` can make; `env=` / `pass_env=` give it a provider's own environment tier. Needs no shell named: the sandbox says which (ADR-0036) |
| `WORKS_UNTIL_STOPPED` | a `ShellAgent` script that commits `first`, leaves `wip.txt`, prints `ready`, then works until it is stopped — what a cancelled run's preservation branch should hold |
| `MAKES_A_MERGE` | a `ShellAgent` script that ends in a merge commit, so the series is nonlinear and collect refuses it — append `exit <n>` to make the agent fail too |
| `PROMPT` | the prompt `a_run` uses; has a second line, so a test can prove the body stayed unlogged |
| `OK_OUTCOME` | the Outcome a scripted agent reports when the test doesn't care |
| `OUTCOME` / `OK_OUTCOME_LINE` | the marker prefix `ShellAgent` parses, and a ready-made reporting line |
| `USAGE` | the prefix of a line `ShellAgent` parses into an `AgentUsage` — `AgentUsage`'s fields as JSON — for a test about reported token usage |
| `recorded_claude(scenario)` / `RECORDED_CLAUDE` / `Total` | the stdout lines of a real Claude Code run, the directory they live in, and the Outcome the `success` recording reports — `test_claude_code_live` records and re-validates them; read its docstring before adding a scenario |

Test modules import helpers as a top-level module — `from helpers import git` — because pytest puts the test file's directory on `sys.path`. `mypy_path` in `pyproject.toml` includes `tests` so the type checker resolves it the same way.

**Keep the tables above current.** `test_docs.py` fails if a shared fixture or helper isn't listed here, so adding one means adding its row — the table is what the next agent reads instead of copy-pasting yours.

**Hoist on the second use, not the third.** A fixture or helper used by one module stays in that module. The moment a second module needs it, move it — to `conftest.py` if it's a fixture, `helpers.py` if it's a plain function. And when you catch yourself about to copy setup out of another test module, that *is* the second use: hoist it instead, in the same commit, rather than leaving a third copy for someone else to find.

## Testing a sandbox backend

`SandboxConformance` in `waystation.testing` is the exec contract as tests, and
it ships so that a backend waystation doesn't ship runs the same ones
(ADR-0035). Subclass it with a `backend` fixture; `test_sandbox_conformance.py`
does that three times, for `NoSandbox`, `DockerSandbox` and `ThinHost` — the
third being a backend built from the public surface alone, alone in
`thin_backend.py` so a guard test can read its imports.

**A contract every backend keeps goes in the suite, not in one backend's
module.** A test in `test_docker_sandbox.py` should be about docker: the image's
user, the transports, a container's labels and teardown.

The suite ships, so it cannot import from here: it carries its own copies of
`init_host_repo` and `until`. That is the one duplication this guide endorses
— change either side and look at the other.

**A backend does not remove the workspace.** Core made it and core takes it
away (ADR-0037), so a backend's teardown is about what the backend made — its
container, its copy, its runner — and the suite holds it to leaving `ws.path`
alone. A test that prepares a workspace by hand removes it by hand:
`await ws.remove()`, or `await run.anyway("workspace", ws.remove(), bound=None)`
inside a composed loop.

**Need a shell in a test? Ask the sandbox.** `[*ctx.sandbox.shell, "env"]` runs
under whatever that backend has, on Windows and in a container alike. Naming
`sh` yourself works on exactly one of them (ADR-0036).

## Idiom

- **Test through the public surface.** Import from `waystation`, not from a private module, unless the seam under test *is* internal. A test that reaches into internals breaks on refactors that changed no behaviour — that's the tell.
- **Name the behaviour, not the function.** `test_agent_silence_resets_on_output_lines`, not `test_run_agent_3`. Read the name back as a sentence about the library; if it doesn't say anything a user would care about, the test is probably at the wrong seam.
- **Never wall-sleep to test a bound.** `use_clock(ManualClock())` makes time explicit; `clock.advance(seconds)` wakes the sleepers. A `time.sleep` in a timeout test is a flake waiting for a slow CI box. The same goes for real work a `ManualClock` test waits on before advancing — a commit, a file, a process: poll for the signal itself with `until`, because a sleep long enough on your box is a guess someone else's box loses. Keep the predicate a stat, never a subprocess: a poll that spawns a process every tick blocks the loop and starves the run it is waiting on, which on a two-core Windows runner is its own kind of flake.
- **`asyncio_mode = "auto"`**, so an async test needs no decorator. (Plenty of older tests still carry `@pytest.mark.asyncio`; harmless, not required.)
- **`isolated_tempdir` is autouse** — every test gets its own temp dir, so a workspace never lands in the host's. Don't reach for `tempfile.mkdtemp` directly.
- **Assert on log records, not on rendered text.** `caplog.at_level(level, logger="waystation")`, then filter `caplog.records` by `record.name`. The console's formatting is not the contract; the logger a line goes to, and the level it goes at, are.

## Coverage

The floor is 85% and CI enforces it. It's a floor, not a target — don't write a test to move the number. If a branch is genuinely unreachable on this platform, say so where it lives rather than chasing it.
