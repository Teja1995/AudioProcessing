# Voice Capture Tool — SPACE READY_4 Analog Mission

Local, offline recording app for standardised voice samples (hydration
study, AATC habitat, 22–30 Aug 2026).

- **Requirements:** [claude.md](claude.md) — the source of truth.
- **Design:** [ARCHITECTURE.md](ARCHITECTURE.md) — read §15 first; it is the
  build order.

## Run

```
pip install -r requirements.txt
python -m capture
```

Serves on 127.0.0.1 only and opens the browser itself. No network is used
or needed.

## Tests

```
python -m unittest discover tests
```

## Status

Skeleton: structure, contracts, config, domain layer, and pure storage
helpers are real; hardware/IO bodies raise `NotImplementedError` with their
build-order step. Nothing fails silently.
