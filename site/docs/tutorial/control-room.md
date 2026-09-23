# Rung 5: the control room

`fan_out` runs a batch concurrently and hands you each result as it completes, fastest first. Here the batch spans repos: one `Flow` per checkout on your disk, built with a list comprehension, all in one `fan_out`. A failed run is one result among many, and never stops the others.

```python title="examples/control_room.py"
--8<-- "examples/control_room.py"
```
