"""Verify the GOAL's mandatory two-line Why/How rule for authored Python definitions."""

import ast
from pathlib import Path


# Why: Make the mandatory documentation gate verifiable instead of relying on spot checks.
# How: Inspect every authored definition and its immediately preceding rationale/mechanism lines.
def main() -> None:
    failures = []
    root = Path(__file__).resolve().parents[1]
    for folder in ("src", "scripts", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            source = path.read_text()
            lines = source.splitlines()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                before = lines[node.lineno - 3 : node.lineno - 1]
                if (
                    len(before) != 2
                    or not before[0].strip().startswith("# Why: ")
                    or not before[1].strip().startswith("# How: ")
                ):
                    failures.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")
    if failures:
        raise SystemExit("Missing Why/How comments:\n" + "\n".join(failures))
    print("All authored Python functions and classes have immediate Why/How comments.")


if __name__ == "__main__":
    main()
