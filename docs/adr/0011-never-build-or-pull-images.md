---
status: accepted
---

# Waystation never builds or pulls images

A sandbox spec names an image; waystation never builds or pulls one — not on the run path, not as a helper. Preflight validates every distinct spec in a batch once, before any run starts, and fails fast with a build hint when an image is missing. Why: fan-out would produce a thundering herd of builds; a ~47 s build inside a ~1.2 s start muddies every timeout; and auto-build solves the rare case (image missing) rather than the common one (image stale). Pre-building one image per flow off the run path is the taught model.

Decided in [wayfinder tickets 5](https://github.com/jeffrichley/waystation/issues/5) and [10](https://github.com/jeffrichley/waystation/issues/10).
