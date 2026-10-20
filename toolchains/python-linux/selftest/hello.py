"""Hallo-Welt für den Selbsttest beim VM-Aufbau."""

import platform
import sys


def greeting() -> str:
    return "Hallo aus der Sandbox (Python)"


if __name__ == "__main__":
    print(greeting())
    print(f"Python {sys.version.split()[0]} auf {platform.platform()}")
