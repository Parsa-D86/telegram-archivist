"""Terminal prompts. Ctrl+C or Ctrl+D at any prompt raises StopRun so the app can exit cleanly."""
from __future__ import annotations

import getpass


class StopRun(Exception):
    """The user pressed Ctrl+C or Ctrl+D at a prompt, or chose to quit."""


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise StopRun from None
    return answer or (default or "")


def ask_secret(prompt: str) -> str:
    """Like ask(), but the typed text is not echoed."""
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise StopRun from None


def ask_yes_no(prompt: str, default: bool | None = None) -> bool:
    hint = "[Y/n]" if default is True else "[y/N]" if default is False else "[y/n]"
    while True:
        answer = ask(f"{prompt} {hint}").lower()
        if not answer and default is not None:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please type y or n.")


def ask_choice(prompt: str, choices: dict[str, str]) -> str:
    """Ask until the answer is one of `choices` (key -> description). Returns the key."""
    options = ", ".join(f"[{key}] {text}" for key, text in choices.items())
    while True:
        answer = ask(f"{prompt} {options}").lower()
        if answer in choices:
            return answer
        print(f"Please type one of: {', '.join(choices)}.")
