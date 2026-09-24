#!/usr/bin/env python3
"""Simple entry point for the Paper Auto application.

Running without arguments starts the normal daily scan. Existing CLI commands
can also be passed through, for example: ``python main.py doctor``.
"""
from __future__ import annotations

import sys

from paper_trend.cli import main as cli_main


COMMANDS = {
    "daily",
    "weekly",
    "top",
    "fetch",
    "translate",
    "doctor",
    "telegram-test",
    "email-test",
    "email-topics",
    "email-papers",
    "semantic-prefetch",
    "bot",
    "search",
}


def normalized_arguments(arguments: list[str]) -> list[str]:
    if not arguments:
        return ["daily"]
    if arguments[0] in {"-h", "--help"}:
        return arguments
    if arguments[0] == "--verbose":
        if len(arguments) == 1:
            return ["--verbose", "daily"]
        if arguments[1] in COMMANDS or arguments[1] in {"-h", "--help"}:
            return arguments
        return ["--verbose", "daily", *arguments[1:]]
    if arguments[0] in COMMANDS:
        return arguments
    return ["daily", *arguments]


if __name__ == "__main__":
    raise SystemExit(cli_main(normalized_arguments(sys.argv[1:])))
