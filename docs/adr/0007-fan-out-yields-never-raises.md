---
status: accepted
---

# Fan-out yields typed results as runs complete and never raises

`fan_out` starts every run and yields each result as it completes — `RunSucceeded` / `RunConflicted` / `RunFailed` — with no cancel-on-failure and nothing raised mid-iteration; an optional concurrency cap, unlimited by default. Why: the flow script owns the parallelism, so it owns retry and abort policy, and it can only do that if every run finishes and reports. Gather-all is a list comprehension, not a second API.

Decided in [wayfinder ticket 3](https://github.com/jeffrichley/waystation/issues/3).
