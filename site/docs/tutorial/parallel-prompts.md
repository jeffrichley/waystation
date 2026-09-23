# Rung 6: parallel prompts

The flagship: a typer command line that fans any number of prompts out to one agent each, landing every agent's commits on one shared batch branch under a live `Dashboard`. Nothing in it is new machinery, except one idea: **a conflict is resolved by another run.** A `RunConflicted` gets a resolver run, based on the batch branch as it stands, that replays the conflicted series with the instructions in [`examples/prompts/resolve.md`](https://github.com/jeffrichley/waystation/blob/main/examples/prompts/resolve.md).

```python title="examples/parallel_prompts.py"
--8<-- "examples/parallel_prompts.py"
```
