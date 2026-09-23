# Rung 3: the primitives

`await flow.run(...)` is a loop of five stages, and every stage is a public function: `prepare_workspace`, `backend.start`, `run_agent`, `collect` and `integrate`. This rung composes them by hand through the `stages` runner, which keeps a run's guarantees: per-stage bounds, and a Ctrl-C held until the stage's own work has ended. A primitive raises `StageError` carrying the same `Failure` an awaited run would return.

```python title="examples/primitives.py"
--8<-- "examples/primitives.py"
```
