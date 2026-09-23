# Rung 7: bring your own agent

An agent provider is a small class you can write yourself: `ClaudeCode` has no special path into waystation, and any object with `preflight`, `command` and `parse` works as `agent=`. This one drives the Cursor agent CLI, which has no schema output of its own, so it reports its Outcome with the marker-line helpers `outcome_instructions` and `find_outcome`. It needs the `waystation-cursor` image (`just example-image cursor`) and `CURSOR_API_KEY`.

```python title="examples/bring_your_own_agent.py"
--8<-- "examples/bring_your_own_agent.py"
```
