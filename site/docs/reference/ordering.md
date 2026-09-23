# `waystation.ordering`

::: waystation.ordering
    options:
      members: false
      show_root_heading: false

<!-- api: waystation.ordering -->

## What a run queue offers

A `queue()` asks its strategy to pick among the runs waiting on it, each as a
`QueuedRun` handle whose `spec` says which run it is (ADR-0048).

::: waystation.queue.QueuedRun
