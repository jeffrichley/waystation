---
status: stable
type: adr
---

# The sandbox says which shell it has, and what else it holds is not probed

`Sandbox` carries `shell`, the argv prefix a command string follows — `("sh", "-c")` in a container, the host's own absolute sh for a backend running host processes. A caller with a script to run asks the sandbox rather than spelling one: `AgentCommand` names either an `argv` or a `script`, and `run_agent` composes `[*sandbox.shell, script]`. `clone_in` asks the same way. `host_shell()` answers it for a host backend and is public beside `host_processes()`.

What a sandbox *otherwise* holds — git, base64, `sleep infinity`, a writable `/tmp`, the agent's own binary — stays written down rather than checked. An agent exiting 127 is logged with what that conventionally means: a shell could not find the command, and if that is what happened the sandbox has to bring it, because waystation installs nothing (ADR-0011).

Why the shell moved: `ScriptedAgent` put the host's absolute sh into its argv and took a `shell="sh"` argument for the case where that was wrong, which every `DockerSandbox` user had to know to pass. That is the mistake ADR-0029 already named for collect — "a host path means nothing inside a container" — and the provider is the one party that cannot answer the question, because ADR-0018 makes it pure and it never sees a sandbox. The sandbox always can. Nineteen `shell="sh"` arguments went out of the tests with it, along with a test double whose whole job was rewriting `sh` to the host's, and the conformance suite's `shell` fixture from ADR-0035 — that fixture existed only because this seam did not.

Why an argv prefix and not a path: a shell that needs arguments of its own can say so, and the shape is the one Dockerfile's `SHELL` instruction already takes. Ansible's connection plugins and Fabric settle it the same way — the thing that owns the connection owns the shell, and the caller hands over a command string.

Why finding the host's sh is a function and not a `ProcessStrategy` method: it searches rather than branches. `shutil.which("sh")`, then beside git, because Git for Windows puts only `Git\cmd` on `PATH` while shipping an sh in `Git\bin`. There is no platform `if` to turn into a strategy, and widening a protocol users implement to add one would be an interface tax (`architecture.md`). It lives in `sandbox/host.py`, already "what every sandbox backend does on the host", rather than in `processes.py`, which is about how a process is spawned and killed.

Why `preflight` asks the host's git first — every run needs git whatever its agent and sandbox, so it is the answer worth giving when more than one is true, and a batch that cannot run at all no longer pays a docker daemon ping to find that out. On Windows the two coincide outright: the sh `host_shell` falls back to is the one Git for Windows ships. A test holds the order.

## Amended by #101: found when asked, and nameable

As first written this decision forced twice, and gave no way out. `NoSandbox.preflight()` refused a host without an sh, and `NoSandbox.start()` resolved one every time — so a `ClaudeCode` run, which names an argv and never wants a shell, could not start on such a host. That is the checking this same ADR turns down for an image, one paragraph earlier, applied to the host instead. And `host_shell()` picked whatever it found, with no way to ask for a different one, which is the number-without-a-knob CLAUDE.md rules out.

So: `Sandbox.shell` is a read-only member, resolved when something reads it and not before; `NoSandbox` preflights nothing; and **`WAYSTATION_SH`** names a shell for a host whose own is somewhere unusual.

The override is an environment variable and **not** an argument on `NoSandbox`, because a constructor argument puts a machine-specific path into the flow script, which is the portability the rest of this decision just bought. Every tool with this problem lands in the same place: Bazel's `BAZEL_SH` is an environment variable and never a `BUILD` file entry; Ansible's `ansible_shell_executable` is an *inventory* variable, so the playbook stays portable; npm's `script-shell` lives in `.npmrc`, and the standing request to allow it in `package.json` is still declined. waystation has no inventory layer — a sandbox spec lives in the flow script by design (ADR-0003) — so Bazel's shape is the one that fits.

`DockerSandbox` gets no override: which shell an image has is the image's property, and Docker's own answer is the `SHELL` instruction. `sh` is already among the things the image must bring (ADR-0028).

## Considered options

- **The git-alias trick, as collect uses (ADR-0029).** `git -c 'alias.x=!<script>' x` runs a script under the shell git was built with, in a container and on Windows alike, and needs no new surface at all. Rejected: it is a workaround for the absent seam, not the seam. It is obscure enough to need a comment at every call site, it does nothing for a user whose own provider runs a script, and it would have left ADR-0035's conformance suite still asking to be told which shell it is talking to. Collect keeps it: collect needs git for the work itself, so the alias costs it nothing.
- **Pass the shell into `AgentProvider.command(prompt, outcome_schema, shell)`.** No invalid states on `AgentCommand`. Rejected: it changes the signature of every provider, including ones users have already written, to carry a parameter most of them ignore — and story 107 keeps a provider to three small jobs.
- **A `shell=True` mode on `Sandbox.exec`.** Rejected for the reason ADR-0028 turned down bytes on stdin and ADR-0010 states generally: every backend would implement it, for a composition its callers can do themselves from one attribute.
- **A `shell` field on `DockerSandbox` for an image whose shell is not `sh`.** No ticket asks, and `sh` is already among the things the image must bring (ADR-0028). It can be added the day an image needs it.
- **A tagged union in place of `AgentCommand`'s two fields** — the shape the `Failure` union already uses here, which would make "an argv or a script" unrepresentable-wrong rather than refused at construction. Rejected for now: `AgentCommand` is a name users' providers already construct, and turning it into a union renames the thing they build, for a gain a five-line `__post_init__` and one test already cover. Worth revisiting if a third way to say what to run arrives.

## What is not checked, and what would change that

A provider's declared needs are not checked against a sandbox. Checking means starting one, because what an image holds is only knowable inside it — and **ADR-0018** keeps preflight to the host, on the grounds that it should be fast and should fail on a typo in seconds. A probe would cost a container start per distinct (agent, sandbox) pair before any run begins, and it can fail for reasons that have nothing to do with what it was asking.

So the cost is paid late instead: a missing `claude` is `AgentExited(127)` after a sandbox has started. What this decision adds is a line saying what 127 usually means, passing on whatever the shell said and pointing at ADR-0011, rather than leaving a reader to interpret an exit code. It cannot name the image — `run_agent` never sees one — and it cannot be certain, because an agent may exit 127 meaning something else. It is the honest half of a check, not the check.

Reopen it when any of these is true: a batch commonly mixes several (agent, sandbox) pairs, so the probe amortises badly and the late failure costs real money; or a sandbox backend arrives that can answer "have you got X" without starting anything; or #43 needs the declared needs as data for some other reason, at which point declaring them is no longer surface bought for one use. The shape it would take is the one #77 sketched: needs declared on the provider and the backend, checked once per distinct pair, failing with the binary and the image named.

## Consequences

- **Surface added:** `Sandbox.shell` — a member of a user-implemented protocol (story 110), so a backend written before this breaks, and ADR-0035's conformance suite is what says so; `AgentCommand.script` and `AgentCommand.argv_in`; and `host_shell` in `waystation.sandbox`. Top-level `waystation` gains nothing. **Surface removed:** `ScriptedAgent.shell` and the suite's `shell` fixture. The net is smaller than before.
- `script` is `AgentCommand`'s **last** field, so every positional construction that worked before binds the same way.
- `AgentCommand` can now be built wrong — neither `argv` nor `script`, or both. `__post_init__` refuses it by name; a test holds the message.
- `ScriptedAgent` lost `shell=` and its host-sh lookup, and its `preflight` does nothing. `ShellAgent` and `a_run` in the tests lost theirs.
- `clone_in` no longer names `sh`, which deleted `test_clone_in.py`'s `_HostSh` double entirely.
- `preflight` checks the host's git before agents and sandboxes. A batch with a bad docker daemon *and* no git now reports git, and stops before pinging the daemon.
- `NoSandbox` preflights nothing (#101). A host without an sh runs any provider that names an argv; one that names a script fails when the shell is read, saying to set `WAYSTATION_SH`.

Decided in [#77](https://github.com/jeffrichley/waystation/issues/77).
