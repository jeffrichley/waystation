# Rung 2: observability

Rung 1's run, with the lights on. Waystation prints nothing on its own: what you see comes from `configure_logging()`, from hooks (plain functions a run calls at named points, here streaming the agent's narration), and from hook bundles such as `EventLog`, which writes every lifecycle event to a file. The built-in bundles use the same hooks yours do. The run lands on the branch `agents/observability`.

```python title="examples/observability.py"
--8<-- "examples/observability.py"
```
