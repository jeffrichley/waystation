# Rung 4: implement, then review

A flow is a sequence of runs, and the Outcome of one decides what the script does next. An implement run lands on a fresh branch; a review run integrates nothing and reports a typed `Verdict`; on a rejection the script makes one bounded fix and one re-review. `if verdict.approved` is ordinary Python. The review instructions are a reusable prompt file, [`examples/prompts/review.md`](https://github.com/jeffrichley/waystation/blob/main/examples/prompts/review.md).

```python title="examples/implement_then_review.py"
--8<-- "examples/implement_then_review.py"
```
