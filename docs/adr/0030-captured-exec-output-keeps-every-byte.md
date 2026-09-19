---
status: accepted
---

# Captured exec output keeps every byte; what people read shows U+FFFD

`Sandbox.exec` still takes text and returns text, but its captured output is exact. The runner decodes it as UTF-8 with `surrogateescape`, so a byte that is not UTF-8 becomes a surrogate escape, and `_git.encode` turns it back into the same byte. `stdin` is encoded the same way. Anything a person or a parser reads shows that byte as U+FFFD instead. That covers line callbacks, the tails `capture=False` keeps, the DEBUG lines of a setup exec, and every `bound_tail`, so `CommandFailed.stderr_tail` too.

Why: a patch does not have to be UTF-8. Git treats a latin-1 file as text, because only a NUL makes a file binary, so `format-patch` writes its bytes raw. Collect cuts the series from an exec's stdout, and the runner used to decode that with `errors="replace"`. Every such byte reached the host as `EF BF BD`, on `NoSandbox` and on `DockerSandbox`, and the run still succeeded. Host git already decoded with `surrogateescape` (`_git.decode`). The sandbox path now matches it.

Why decode twice: a surrogate escape keeps the byte, but it can't be printed. Printing to a UTF-8 stream, writing to a UTF-8 log file and dumping a pydantic model to JSON all reject a lone surrogate, and the error would show up far from the byte that caused it. Pydantic validates one without complaint, so an Outcome would carry it until something tried to write it out. Captured output goes to git. Everything else goes to people, or to an agent provider's `parse`, whose output becomes the Outcome.

## Considered options

- **Bytes on `Sandbox.exec`.** Rejected for the same reason ADR-0028 rejected bytes on stdin: every backend implements that contract, and changing it for one caller isn't worth it (ADR-0010).
- **Base64 the series inside the sandbox**, as `clone_in` does with the bundle it sends in. Rejected. It protects only the exec that carries it: the squash's diff coming out, its `git apply` going back in, and any later caller would each need the same treatment. It also makes every series a third bigger, for a case most series never hit. Its one advantage is that it would keep collect safe from a third-party backend that decodes with `replace`. The protocol's docstring states the contract instead.
- **`surrogateescape` everywhere, callbacks included.** Rejected: hooks, log handlers and an Outcome dumped to JSON would receive text they cannot encode.
- **Latin-1 for captured output.** It round-trips too. Rejected: it garbles every non-ASCII character of ordinary UTF-8 output, stderr included, and host git's `decode` already uses `surrogateescape`.

## Consequences

- A backend not built on `HostRunner` has to keep this contract itself, and `Sandbox.exec`'s docstring says so. A backend that decodes with `replace` loses the bytes silently, as the runner did.
- `PatchSeries.patches` can hold surrogate escapes, as `PatchSeries.from_range` already could. To print such a patch, the stream needs an error handler such as `backslashreplace`.
- `bound_tail` used to raise `UnicodeEncodeError` on a surrogate escape. Host git's stderr could already contain one, so a failing host git that printed a non-UTF-8 byte raised that instead of its `CommandFailed`. It now raises its `CommandFailed`.
- `test_patch_bytes.py` and the docker tier check that the bytes arrive intact. `test_hooks.py`, `test_patch_bytes.py` and `test_tails.py` check that readers get U+FFFD.

Decided in [#68](https://github.com/jeffrichley/waystation/pull/68).
