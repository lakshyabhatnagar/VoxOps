"""Check that every Python function and class has at least two explanation lines."""

import ast
from pathlib import Path


# Checks that every Python function and class has explanation lines directly above it.
# Prints the file and name of anything missing them, including methods and nested functions.
# Allows longer explanations while accepting the two-line comments already in the project.
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
                    or any(not line.strip().startswith("# ") for line in before)
                    or any(not line.strip()[2:].strip() for line in before)
                    or any(line.strip().startswith(("# Why:", "# How:")) for line in before)
                ):
                    failures.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")
    if failures:
        raise SystemExit(
            "Missing at least two plain-English explanation lines:\n" + "\n".join(failures)
        )
    print("All Python functions and classes have at least two explanation lines above them.")


if __name__ == "__main__":
    main()
