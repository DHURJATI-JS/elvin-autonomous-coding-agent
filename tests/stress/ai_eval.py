"""AI accuracy evaluation with REAL models (Gemma 3 via the gateway; Qwen/Claude/
ChatGPT when configured). Uses Elvin's own prompts, router, parser and sandbox:

  coding:  20 tasks -> CODER prompt -> parse -> run hidden pytest tests in the
           sandbox -> on failure one FIXER round with the test output (like the
           agent's fix loop) -> accuracy first-try and after the fix round
  planner: 10 requests -> valid plan JSON with the required fields
  vision:  4 generated images -> valid design JSON naming the right main colour

Writes /stress/out/ai_eval.json."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import time

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, "/app")
from elvin.services import selfcheck  # noqa: E402
from elvin.services.agent import FORMAT_REMINDER  # noqa: E402
from elvin.services.ai import blocks, prompts  # noqa: E402
from elvin.services.ai import router as ai  # noqa: E402
from elvin.services.ai.providers import ImageInput  # noqa: E402
from elvin.services.fs import PathError, normalize_rel  # noqa: E402

TEAM = os.environ.get("EVAL_TEAM", "gemma_qwen")
CODER_CHAIN = [k for k in os.environ.get("EVAL_CODER_CHAIN", "").split(",") if k] or None   # e.g. "gemma" to time Gemma as the coder
RUNNER = os.environ["RUNNER_URL"]
TOKEN = os.environ["RUNNER_TOKEN"]
OUT = os.environ.get("EVAL_OUT", "/stress/out/ai_eval.json")

TASKS: list[tuple[str, str, str]] = [
    ("is_palindrome", "Write solution.py with is_palindrome(s: str) -> bool that ignores case and every non-alphanumeric character.",
     "from solution import is_palindrome\ndef test_p():\n    assert is_palindrome('A man, a plan, a canal: Panama')\n    assert is_palindrome('')\n    assert not is_palindrome('hello')\n    assert is_palindrome('No lemon, no melon')\n"),
    ("fizzbuzz", "Write solution.py with fizzbuzz(n: int) -> list[str] returning the strings for 1..n: 'Fizz' for multiples of 3, 'Buzz' for 5, 'FizzBuzz' for both, otherwise the number as a string.",
     "from solution import fizzbuzz\ndef test_f():\n    assert fizzbuzz(5) == ['1','2','Fizz','4','Buzz']\n    assert fizzbuzz(15)[-1] == 'FizzBuzz'\n    assert fizzbuzz(0) == []\n"),
    ("word_count", "Write solution.py with word_count(text: str) -> dict[str, int] counting words case-insensitively; words are runs of letters, digits or apostrophes.",
     "from solution import word_count\ndef test_w():\n    assert word_count('Hello, hello world!') == {'hello': 2, 'world': 1}\n    assert word_count('') == {}\n    assert word_count(\"It's it's IT'S\") == {\"it's\": 3}\n"),
    ("flatten", "Write solution.py with flatten(items: list) -> list that flattens arbitrarily nested lists (not strings or tuples).",
     "from solution import flatten\ndef test_fl():\n    assert flatten([1, [2, [3, [4]]], 5]) == [1, 2, 3, 4, 5]\n    assert flatten([]) == []\n    assert flatten(['ab', ['c']]) == ['ab', 'c']\n"),
    ("roman_to_int", "Write solution.py with roman_to_int(s: str) -> int converting a Roman numeral (I..MMMCMXCIX) to an integer.",
     "from solution import roman_to_int\ndef test_r():\n    assert roman_to_int('III') == 3\n    assert roman_to_int('LVIII') == 58\n    assert roman_to_int('MCMXCIV') == 1994\n"),
    ("int_to_roman", "Write solution.py with int_to_roman(n: int) -> str converting 1..3999 to a Roman numeral; raise ValueError outside that range.",
     "import pytest\nfrom solution import int_to_roman\ndef test_i():\n    assert int_to_roman(1994) == 'MCMXCIV'\n    assert int_to_roman(4) == 'IV'\n    with pytest.raises(ValueError):\n        int_to_roman(0)\n"),
    ("merge_intervals", "Write solution.py with merge_intervals(intervals: list[list[int]]) -> list[list[int]] that merges overlapping (or touching) intervals and returns them sorted by start.",
     "from solution import merge_intervals\ndef test_m():\n    assert merge_intervals([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]\n    assert merge_intervals([[1,4],[4,5]]) == [[1,5]]\n    assert merge_intervals([]) == []\n"),
    ("brackets", "Write solution.py with is_balanced(s: str) -> bool checking that (), [] and {} are balanced and correctly nested; other characters are ignored.",
     "from solution import is_balanced\ndef test_b():\n    assert is_balanced('{[()()]}')\n    assert not is_balanced('([)]')\n    assert is_balanced('a(b)c')\n    assert not is_balanced('((')\n"),
    ("top_k", "Write solution.py with top_k_frequent(nums: list[int], k: int) -> list[int] returning the k most frequent numbers, most frequent first; ties go to the smaller number.",
     "from solution import top_k_frequent\ndef test_t():\n    assert top_k_frequent([1,1,1,2,2,3], 2) == [1, 2]\n    assert top_k_frequent([4,4,5,5,6], 2) == [4, 5]\n    assert top_k_frequent([7], 1) == [7]\n"),
    ("parse_csv", "Write solution.py with parse_csv_line(line: str) -> list[str] parsing one CSV line: fields separated by commas, fields may be wrapped in double quotes, and inside quotes a doubled quote \"\" means a literal quote. Do not use the csv module.",
     "from solution import parse_csv_line\ndef test_c():\n    assert parse_csv_line('a,b,c') == ['a','b','c']\n    assert parse_csv_line('\"x,y\",z') == ['x,y','z']\n    assert parse_csv_line('\"say \"\"hi\"\"\",2') == ['say \"hi\"','2']\n    assert parse_csv_line('') == ['']\n"),
    ("chunk", "Write solution.py with chunk(items: list, size: int) -> list[list] splitting a list into consecutive chunks of the given size (last chunk may be shorter); raise ValueError if size < 1.",
     "import pytest\nfrom solution import chunk\ndef test_ch():\n    assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\n    assert chunk([], 3) == []\n    with pytest.raises(ValueError):\n        chunk([1], 0)\n"),
    ("lru", "Write solution.py with a class LRUCache(capacity: int) having get(key) -> value or -1, and put(key, value) that evicts the least recently used key when full.",
     "from solution import LRUCache\ndef test_l():\n    c = LRUCache(2)\n    c.put(1, 1); c.put(2, 2)\n    assert c.get(1) == 1\n    c.put(3, 3)\n    assert c.get(2) == -1\n    c.put(4, 4)\n    assert c.get(1) == -1 and c.get(3) == 3 and c.get(4) == 4\n"),
    ("binary_search", "Write solution.py with binary_search(arr: list[int], target: int) -> int returning the index of target in the sorted list arr, or -1.",
     "from solution import binary_search\ndef test_bs():\n    assert binary_search([1,3,5,7,9], 7) == 3\n    assert binary_search([1,3,5], 4) == -1\n    assert binary_search([], 1) == -1\n"),
    ("slugify", "Write solution.py with slugify(text: str) -> str: lowercase, every run of characters that are not a-z or 0-9 becomes one hyphen, no leading or trailing hyphens.",
     "from solution import slugify\ndef test_s():\n    assert slugify('Hello, World!') == 'hello-world'\n    assert slugify('  Many   spaces  ') == 'many-spaces'\n    assert slugify('---') == ''\n"),
    ("median", "Write solution.py with median(nums: list[float]) -> float; raise ValueError for an empty list. For an even count return the mean of the two middle values.",
     "import pytest\nfrom solution import median\ndef test_md():\n    assert median([3,1,2]) == 2\n    assert median([4,1,3,2]) == 2.5\n    with pytest.raises(ValueError):\n        median([])\n"),
    ("anagrams", "Write solution.py with group_anagrams(words: list[str]) -> list[list[str]]: each group sorted alphabetically, groups sorted by their first word.",
     "from solution import group_anagrams\ndef test_a():\n    assert group_anagrams(['eat','tea','tan','ate','nat','bat']) == [['ate','eat','tea'], ['bat'], ['nat','tan']]\n    assert group_anagrams([]) == []\n"),
    ("rle", "Write solution.py with rle_encode(s: str) -> str ('aaabcc' -> '3a1b2c') and rle_decode(s: str) -> str reversing it (counts may have several digits).",
     "from solution import rle_encode, rle_decode\ndef test_rle():\n    assert rle_encode('aaabcc') == '3a1b2c'\n    assert rle_decode('3a1b2c') == 'aaabcc'\n    assert rle_decode(rle_encode('x' * 12)) == 'x' * 12\n    assert rle_encode('') == ''\n"),
    ("stack", "Write solution.py with a class Stack having push(x), pop() (raises IndexError when empty), peek() (raises IndexError when empty), is_empty() and __len__.",
     "import pytest\nfrom solution import Stack\ndef test_st():\n    s = Stack()\n    assert s.is_empty() and len(s) == 0\n    s.push(1); s.push(2)\n    assert s.peek() == 2 and s.pop() == 2 and len(s) == 1\n    s.pop()\n    with pytest.raises(IndexError):\n        s.pop()\n"),
    ("transpose", "Write solution.py with transpose(matrix: list[list]) -> list[list] returning the transposed matrix (an empty matrix gives []).",
     "from solution import transpose\ndef test_tr():\n    assert transpose([[1,2,3],[4,5,6]]) == [[1,4],[2,5],[3,6]]\n    assert transpose([]) == []\n    assert transpose([[1]]) == [[1]]\n"),
    ("caesar", "Write solution.py with caesar(text: str, shift: int) -> str shifting letters A-Z and a-z by shift (wrapping, negative shifts allowed), keeping case and leaving other characters unchanged.",
     "from solution import caesar\ndef test_ca():\n    assert caesar('Hello, World!', 3) == 'Khoor, Zruog!'\n    assert caesar('abc', -1) == 'zab'\n    assert caesar(caesar('Round trip', 13), 13) == 'Round trip'\n"),
]
PLANNER_PROMPTS = [
    "Add a /health endpoint to my FastAPI app", "What does a Python virtual environment do?", "Create a CLI that converts CSV to JSON",
    "Write unit tests for the date parser", "Explain the difference between threads and processes", "Build a small Flask app with login using flask-login",
    "Refactor main.py into modules", "Add rate limiting using the slowapi library", "Is my project using any database?", "Create a README for this project",
]


def bundle(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def run_tests(client: httpx.AsyncClient, files: dict[str, str]) -> tuple[bool, str]:
    r = await client.post(f"{RUNNER}/run", headers={"X-Runner-Token": TOKEN}, files={"bundle": ("p.tar.gz", bundle(files), "application/gzip")},
                          data={"command": "python -m pytest -q -p no:cacheprovider test_hidden.py", "timeout": "60"}, timeout=120)
    res = r.json()
    return res.get("exit_code") == 0, (res.get("stdout", "") + res.get("stderr", ""))[-3000:]


def step_message(prompt: str, files_text: str = "EXISTING FILE CONTENTS: (none needed)") -> str:
    return (f"PROJECT: eval\n{prompts.platform_note()}\n\nPROJECT MEMORY (things the user told you before):\n(none yet)\n\n"
            f"FILES:\n(the project is empty)\n\nUSER REQUEST:\n{prompt}\n\nPLAN:\n1. Write the code\n\nCURRENT STEP 1/1: Write the code\n{prompt}\n\n"
            f"APPROVED LIBRARIES:\n(none - use only the standard library or what the project already has)\n\n{files_text}")


def extract(text: str, finish: str) -> tuple[dict[str, str], bool]:
    parsed = blocks.parse(text)
    files: dict[str, str] = {}
    for path, content in parsed.files + (parsed.incomplete if finish == "stop" else []):
        try:
            files[normalize_rel(path)] = content
        except PathError:
            continue
    return files, bool(parsed.files) and not parsed.incomplete


async def generate(system: str, task_role: str, message: str):
    """Like the agent's _generate: files that don't parse (or code without FILE blocks) get one repair round."""
    res = await ai.call("coder", team=TEAM, system=system, task_role=task_role, max_tokens=3000, chain=CODER_CHAIN,
                        messages=[{"role": "user", "content": message}])
    files, fmt = extract(res.text, res.finish)
    problems = [p for p in (selfcheck.syntax_problem(k, v) for k, v in files.items()) if p]
    if not files and "```" in res.text:
        problems.append(FORMAT_REMINDER)
    repaired = False
    if problems:
        retry = (message + "\n\nYOUR PREVIOUS ANSWER HAD PROBLEMS - fix them and send the complete answer again:\n- "
                 + "\n- ".join(problems))
        res = await ai.call("coder", team=TEAM, system=system, task_role=task_role, max_tokens=3000, chain=CODER_CHAIN,
                            messages=[{"role": "user", "content": retry}])
        files2, _ = extract(res.text, res.finish)
        if files2:
            files, repaired = {**files, **files2}, True
    return res, files, fmt, repaired


async def coding_task(sem, client, name, prompt, test):
    async with sem:
        started = time.monotonic()
        rec = {"task": name, "first_try": False, "after_fix": False, "format_ok": False, "self_repaired": False, "provider": "",
               "seconds": 0.0, "error": ""}
        try:
            res, files, rec["format_ok"], rec["self_repaired"] = await generate(prompts.CODER, "coder", step_message(prompt))
            rec["provider"] = res.provider
            files = {k: v for k, v in files.items() if k != "test_hidden.py"}
            ok, output = await run_tests(client, {**files, "test_hidden.py": test})
            rec["first_try"] = ok
            if not ok:
                ctx = "EXISTING FILE CONTENTS:\n" + "\n".join(f"----- {p} -----\n{t}" for p, t in files.items())
                fix_msg = (step_message(prompt, ctx) + f"\n\nCOMMAND: python -m pytest -q\nEXIT CODE: 1\nOUTPUT:\n{output}")
                _res2, files2, _fmt, _rep = await generate(prompts.FIXER, "fixer", fix_msg)
                files.update({k: v for k, v in files2.items() if k != "test_hidden.py"})
                ok, output = await run_tests(client, {**files, "test_hidden.py": test})
                if not ok:
                    rec["error"] = output[-400:]
            rec["after_fix"] = ok
        except Exception as exc:
            rec["error"] = f"{exc.__class__.__name__}: {exc}"[:400]
        rec["seconds"] = round(time.monotonic() - started, 1)
        print(f"  {name:16} first={rec['first_try']!s:5} fixed={rec['after_fix']!s:5} format={rec['format_ok']!s:5} {rec['seconds']}s", flush=True)
        return rec


async def planner_task(sem, prompt):
    async with sem:
        try:
            res = await ai.call("planner", team=TEAM, system=prompts.PLANNER, task_role="planner", json_mode=True, max_tokens=1200,
                                messages=[{"role": "user", "content": f"PROJECT: demo\n{prompts.platform_note()}\n\nFILES:\nmain.py (1200 B)\n\nUSER REQUEST:\n{prompt}"}])
            d = res.data or {}
            ok = isinstance(d.get("reply"), str) and isinstance(d.get("steps", []), list) and "needs_changes" in d and isinstance(d.get("libraries", []), list)
            return {"prompt": prompt, "valid": ok, "needs_changes": d.get("needs_changes"), "steps": len(d.get("steps") or [])}
        except Exception as exc:
            return {"prompt": prompt, "valid": False, "error": str(exc)[:200]}


async def vision_task(sem, color_name, rgb):
    async with sem:
        im = Image.new("RGB", (640, 400), rgb)
        d = ImageDraw.Draw(im)
        d.rectangle([40, 30, 600, 90], fill=(255, 255, 255))
        d.rectangle([240, 300, 400, 350], fill=(250, 204, 21))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        try:
            res = await ai.call("vision", team=TEAM, system=prompts.VISION, task_role="vision", json_mode=True, max_tokens=800,
                                images=[ImageInput(buf.getvalue())], messages=[{"role": "user", "content": "Describe this design."}])
            text = json.dumps(res.data).lower()
            hexes = [h for h in __import__("re").findall(r"#([0-9a-f]{6})", text)]

            def close(h):
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return abs(r - rgb[0]) + abs(g - rgb[1]) + abs(b - rgb[2]) < 120
            ok = bool(res.data) and (color_name in text or any(close(h) for h in hexes))
            return {"color": color_name, "valid_json": bool(res.data), "color_ok": ok}
        except Exception as exc:
            return {"color": color_name, "valid_json": False, "color_ok": False, "error": str(exc)[:200]}


async def main() -> None:
    reg = ai.registry()
    coder_chain = CODER_CHAIN or reg.chain(TEAM, "coder")
    print("providers:", list(reg.providers), "team:", TEAM, "coder chain:", coder_chain, "planner chain:", reg.chain(TEAM, "planner"), flush=True)
    sem = asyncio.Semaphore(int(os.environ.get("EVAL_CONCURRENCY", "3")))
    started = time.monotonic()
    async with httpx.AsyncClient() as client:
        print("coding tasks:", flush=True)
        coding = await asyncio.gather(*(coding_task(sem, client, n, p, t) for n, p, t in TASKS))
    print("planner JSON:", flush=True)
    planner = await asyncio.gather(*(planner_task(sem, p) for p in PLANNER_PROMPTS))
    print("vision:", flush=True)
    vision = await asyncio.gather(*(vision_task(sem, n, c) for n, c in
                                    [("blue", (37, 99, 235)), ("red", (220, 38, 38)), ("green", (22, 163, 74)), ("purple", (126, 34, 206))]))
    n = len(coding)
    summary = {
        "team": TEAM, "coder_chain": coder_chain, "planner_chain": reg.chain(TEAM, "planner"), "vision_chain": reg.chain(TEAM, "vision"),
        "coder_answered_by": dict(__import__("collections").Counter(r["provider"] or "none" for r in coding)),
        "median_coding_task_s": sorted(r["seconds"] for r in coding)[len(coding) // 2],
        "self_repaired_tasks": sum(bool(r.get("self_repaired")) for r in coding),
        "coding_first_try_pct": round(100 * sum(r["first_try"] for r in coding) / n, 1),
        "coding_after_fix_pct": round(100 * sum(r["after_fix"] for r in coding) / n, 1),
        "format_compliance_pct": round(100 * sum(r["format_ok"] for r in coding) / n, 1),
        "planner_valid_pct": round(100 * sum(r["valid"] for r in planner) / len(planner), 1),
        "vision_valid_json_pct": round(100 * sum(r["valid_json"] for r in vision) / len(vision), 1),
        "vision_color_pct": round(100 * sum(r["color_ok"] for r in vision) / len(vision), 1),
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    print(json.dumps(summary, indent=1), flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"summary": summary, "coding": coding, "planner": planner, "vision": vision}, fh, indent=1)


if __name__ == "__main__":
    asyncio.run(main())
