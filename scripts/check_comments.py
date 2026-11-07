"""Keep the historical check command without enforcing per-function comment counts."""

import ast
from pathlib import Path


def check_sources(root: Path) -> None:
    for folder in ("src", "scripts", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            ast.parse(path.read_text(), filename=str(path))


def main() -> None:
    check_sources(Path(__file__).resolve().parents[1])
    print("Python syntax checked; function and class comment counts are not enforced.")


if __name__ == "__main__":
    main()
