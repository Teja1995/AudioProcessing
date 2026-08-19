"""Audio layer — the ONLY package that imports sounddevice/soundfile.

Keeping it small and flat is the point: this is the only place capable of
violating the "no processing" rule, so it must stay readable at 3 a.m. by a
tired person (CLAUDE.md, Conventions).
"""
