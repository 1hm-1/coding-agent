from __future__ import annotations

import sys

from todo_parser import parse_command


def main() -> int:
    command = parse_command(" ".join(sys.argv[1:]))
    if command is None:
        print("No command")
        return 0
    print(f"{command.action}: {command.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

