"""Release-build helper: copies the tools' .py files with every comment and
docstring removed (the source in the repo keeps them).

Usage:  python build/strip_comments.py <source folder> <output folder>

Goes through Python's own parser (ast) rather than regexes, so '#' or
triple quotes inside strings are never touched, and each stripped file is
compiled again before it's written.
"""
import ast
import sys
from pathlib import Path


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body.pop(0)
            if not body:
                body.append(ast.Pass())


def strip(source: str) -> str:
    tree = ast.parse(source)
    _strip_docstrings(tree)
    out = ast.unparse(tree) + "\n"  # unparse never writes comments
    compile(out, "<stripped>", "exec")
    return out


def main(argv: list) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    src, dst = Path(argv[0]), Path(argv[1])
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.glob("*.py")):
        text = strip(path.read_text(encoding="utf-8-sig"))
        (dst / path.name).write_text(text, encoding="utf-8")
        print(f"{path.name}: {path.stat().st_size:,} -> {len(text.encode()):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
