"""Storage layer — every data-safety guarantee lives here.

Write immediately, never overwrite, append-only ledgers, .partial-then-rename.
Isolated from hardware so all of it is testable without a microphone.
"""
