"""better-errors: a CLI tool that explains runtime errors and proposes safe fixes.

This package wraps commands (or reads piped output), detects tracebacks,
sends redacted context to a configurable LLM provider, and presents a
plain-English root cause plus a reviewable unified diff before touching
any file on disk.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
