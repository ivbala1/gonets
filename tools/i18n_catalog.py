"""Каталог перевода бота: сбор msgid из кода и сверка с locales/<язык>.json.

    python tools/i18n_catalog.py check [--lang en]    # нет перевода, лишние, подстановки
    python tools/i18n_catalog.py missing [--lang en]  # JSON {msgid: ""} без перевода
    python tools/i18n_catalog.py merge FILE [--lang en]  # дописать переводы из FILE

msgid это строковая константа в вызове `_()` или `N_()` (смежные литералы
Python склеивает сам). Подстановки сверяются по именам полей `.format()`.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import string
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "claude_tg_bot"
LOCALES = PACKAGE / "locales"
MARKERS = {"_", "N_"}


def iter_sources(root: Path = PACKAGE):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def extract(paths=None) -> dict[str, list[str]]:
    """msgid -> места в коде (файл:строка)."""
    found: dict[str, list[str]] = {}
    for path in paths if paths is not None else iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in MARKERS and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                found.setdefault(arg.value, []).append(where)
    return found


_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def fields(text: str) -> set[str] | None:
    """Имена полей .format() в тексте; None, если скобки не разбираются.
    Образцы JSON в промптах (`{"items": ...}`) полями не считаются: такие
    строки в .format() не идут, их содержимое переводится свободно."""
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text)
                if name is not None and _FIELD.fullmatch(name)}
    except ValueError:
        return None


def load(lang: str) -> dict[str, str]:
    path = LOCALES / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(lang: str, data: dict[str, str]) -> None:
    LOCALES.mkdir(parents=True, exist_ok=True)
    path = LOCALES / f"{lang}.json"
    text = json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8")


def problems(lang: str = "en") -> dict[str, list]:
    msgids = extract()
    cat = load(lang)
    missing = sorted(m for m in msgids if not cat.get(m))
    stale = sorted(m for m in cat if m not in msgids)
    mismatch = []
    for m in msgids:
        tr = cat.get(m)
        if tr and fields(m) != fields(tr):
            mismatch.append((m, tr, msgids[m][0]))
    return {"missing": missing, "stale": stale, "mismatch": mismatch}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("check", "missing", "merge"))
    ap.add_argument("file", nargs="?")
    ap.add_argument("--lang", default="en")
    args = ap.parse_args(argv)
    if args.cmd == "missing":
        msgids = extract()
        cat = load(args.lang)
        out = {m: "" for m in sorted(msgids) if not cat.get(m)}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "merge":
        if not args.file:
            ap.error("merge: нужен файл с переводами")
        cat = load(args.lang)
        new = json.loads(Path(args.file).read_text(encoding="utf-8"))
        cat.update({k: v for k, v in new.items() if isinstance(v, str) and v})
        msgids = extract()
        save(args.lang, {k: v for k, v in cat.items() if k in msgids})
        print(f"{len(new)} переводов слито, в каталоге {len(cat)}")
        return 0
    res = problems(args.lang)
    print(f"msgid в коде: {len(extract())}; без перевода: {len(res['missing'])}; "
          f"лишних: {len(res['stale'])}; подстановки расходятся: {len(res['mismatch'])}")
    for m, tr, where in res["mismatch"]:
        print(f"  {where}: {m!r} -> {tr!r}")
    return 1 if any(res.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
