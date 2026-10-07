"""Write-free syntax gate for local and CI verification."""

from pathlib import Path
import ast


def main() -> None:
    failures = []
    for path in [*Path("geiter").glob("*.py"), *Path("tests").glob("*.py")]:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{path}:{exc.lineno}:{exc.offset}: {exc.msg}")
    if failures:
        raise SystemExit("\n".join(failures))
    print("syntax: ok")


if __name__ == "__main__":
    main()
