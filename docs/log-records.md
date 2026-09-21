# Log records

Hooks carry most of what happens in a run. Two things they deliberately don't:
the **agent starting**, which opens between `sandbox_ready` and the first
output line, and a **cancellation**, which has no result for `run_end` to be
given (ADR-0017). Both arrive as log records instead, and that makes the
records an interface rather than output — the one a shipped observer reads, and
the one a flow script's own bundle reads, by the same route (ADR-0001).

This page is that interface: the logger names, the levels, and the extras each
record carries. `tests/test_run_records.py` holds it, in both directions — an
event documented here that nothing logs fails, and so does one logged that
nothing documents.

## The run channel

`waystation.run` carries one **INFO** record per lifecycle event. The message
text is for people and may change; the `event` extra is the contract.

| Event | Level | Logged when |
| --- | --- | --- |
| `run_start` | INFO | The run begins, before the workspace is prepared. Also a hook. |
| `workspace_ready` | INFO | The base ref resolved and the workspace is built. Also a hook. |
| `sandbox_ready` | INFO | The sandbox is up and `ctx.sandbox` is usable. Also a hook. |
| `agent_start` | INFO | The prompt is read and the agent is about to exec. **No hook.** |
| `agent_end` | INFO | The agent exited; the record carries its code and elapsed. Also a hook. |
| `integrated` | INFO | A series landed on its target. Also a hook. |
| `run_end` | INFO | The run produced a result — succeeded, failed or conflicted. Also a hook. |
| `cancelled` | INFO | The run was cancelled, and where its series went. **No hook.** |

`agent_start` and `cancelled` are the two to read this channel for. The rest are
here because a channel with holes in it is one you have to leave to answer a
question, and because an event that is also a hook lets a bundle take the whole
lifecycle through one route when that suits it better.

`agent_start` announces the prompt by **shape** — its length, and up to 80
characters of its first line. The body never reaches a record.

A run that is cancelled logs `cancelled` and nothing after it: there is no
`run_end`, because there is no result (ADR-0017).

### Extras

Every record on the channel carries all three:

| Extra | Type | Is |
| --- | --- | --- |
| `event` | `str` | Which of the rows above. Only records on `waystation.run` have it. |
| `run_id` | `str` | The run's id — 8 lowercase hex characters. |
| `run_name` | `str \| None` | What `.name()` set, or `None`. It is `run_name` and not `name` because `name` is the logger's own record attribute, and stdlib refuses to let an extra overwrite it. |

### Reading it

A bundle watches the channel with stdlib logging and nothing else:

```python
import logging
from waystation import HookBundle, RunContext

class WatchForCancellation(HookBundle):
    def on_run_start(self, ctx: RunContext) -> None:
        channel = logging.getLogger("waystation.run")
        channel.setLevel(logging.INFO)          # so the records exist to handle
        channel.addHandler(_Mine(ctx.run_id))
```

Two things make that as reliable as the shipped `RunLogFiles`:

- **Set a level yourself.** Without `configure_logging`, the hierarchy sits at
  the root's WARNING and an INFO record is never *created*, so no handler can
  see it. Turning `waystation.run` up is per-logger tuning, which waystation
  honours everywhere — a logger the script tuned itself reaches the console and
  a host's handlers too (ADR-0026).
- **Take the run's task at `run_start`.** A cancelled run fires no `run_end`,
  so nothing hook-shaped tells you to stop watching. `run_start` fires in the
  task performing the run, which is where `RunLogFiles` takes it; `agent_output`
  does not, since it fires from the exec's reader tasks (ADR-0026).

## The other loggers

These carry lines, not events: no `event` extra, and no promise of one record
per anything. They carry `run_id` and `run_name` like the channel does.

| Logger | Level | Carries |
| --- | --- | --- |
| `waystation.agent` | INFO | An agent provider's own lines — which credential its preflight found. |
| `waystation.agent.output` | DEBUG | Every line the agent emits. An N-way fan-out is unreadable at INFO. |
| `waystation.hook` | INFO | Where `ctx.log` writes, so a hook author's lines stay separable from ours. |
| `waystation.git` | DEBUG | Host git command lines, credentials elided (ADR-0025). |
| `waystation.sandbox` | DEBUG | Sandbox lifecycle and the command lines it execs. |
| `waystation` | ERROR | Waystation itself had a problem it did not raise — a failure after the run already failed (ADR-0024), or a built-in observer that failed and was ignored (ADR-0026). |

### Logging into a run from an adapter of your own

A `SandboxBackend` or `AgentProvider` you wrote gets its lines into a run's
`RunLogFiles` file the same way the shipped ones do — by joining the channel:

```python
from waystation import run_logger

_log = run_logger("myco.docker")      # -> waystation.myco.docker

_log.info("reusing the warm container")
```

The name lands **under** `waystation`, and that is what puts it on the channel:
a run's observers attach to the package logger, and `logging` routes by dotted
name alone, so a logger outside the hierarchy is never reached however it is
tagged. Celery's `get_task_logger` and Prefect's `get_run_logger` parent a
caller's logger for the same reason.

Your own loggers are untouched by this. Keep `myco.docker` for lines that are
your library's business rather than a run's; `run_logger` is only for the ones
that belong to the run someone is watching.

Its records carry `run_id` and `run_name`, picked up from whichever run is
executing, so they sort into the right file in a fan-out. Their level is yours:
`waystation` is held at DEBUG while a run file is open (ADR-0026), so a logger
of yours with no level of its own inherits that and its DEBUG lines land too.

## Turning it on

`configure_logging()` installs the one handler waystation owns, on **stderr** —
stdout is the agent's Outcome channel (ADR-0001). Nothing is installed on
import: a `NullHandler` is all a host application gets until a flow script asks.

```python
from waystation import configure_logging
import logging

configure_logging("INFO")
logging.getLogger("waystation.agent.output").setLevel(logging.DEBUG)
```
