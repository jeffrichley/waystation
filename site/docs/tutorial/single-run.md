# Rung 1: a single run

One real Claude Code agent in a Docker container, against a clone of your repo. The script builds a `Flow`, asks it for a run, awaits it, and matches on the one result it gets back: `RunSucceeded`, `RunConflicted` or `RunFailed`. An awaited run never raises for a failure, so that `match` is the whole of the error handling.

!!! warning "This rung lands on your own HEAD"
    `.integrate("HEAD")` fast-forwards the HEAD of the repo you run it from onto
    the agent's commits. It refuses rather than touch uncommitted work, but read
    the note in the docstring before you run it. Every later rung lands on a
    branch instead.

```python title="examples/single_run.py"
--8<-- "examples/single_run.py"
```
