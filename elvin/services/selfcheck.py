"""Quick, safe checks of the files a model wants to write - nothing is executed.

Small models sometimes paste the test output or prose into a source file while
"fixing" it. Elvin parses Python and strict-JSON files before asking for
permission: a file that doesn't parse triggers one automatic repair round, and if
it still doesn't parse, the file carries a warning in the permission dialog."""
from __future__ import annotations

import ast
import json
import warnings

# JSON files that are allowed to contain comments ("JSONC")
_JSONC = ("tsconfig", "jsconfig", ".vscode/", ".devcontainer/", ".eslintrc", "devcontainer.json")


def syntax_problem(path: str, text: str) -> str | None:
    lower = path.lower()
    try:
        if lower.endswith((".py", ".pyw")):
            with warnings.catch_warnings():          # e.g. invalid escape sequences are warnings, not errors
                warnings.simplefilter("ignore")
                ast.parse(text, filename=path)
        elif lower.endswith(".json") and not any(j in lower for j in _JSONC):
            json.loads(text)
    except SyntaxError as exc:
        return f"'{path}' does not compile: {exc.msg} (line {exc.lineno}). Send only valid code in that file - no test output or explanations."
    except ValueError as exc:
        return f"'{path}' is not valid JSON: {exc}"
    return None
