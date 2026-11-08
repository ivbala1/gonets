"""Преграда для файлов состояния (ревизия повторов 07.11.2026, п. 2): атомарная
запись и запись JSON в файл только через jsonstore. Ручной `os.replace`,
`Path.replace(...)`, `json.dump(..., f)` и `write_text(json.dumps(...))`
в коде бота это новая копия со своими правами и уборкой: звать jsonstore."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Исключения по вызову (файл, текст вызова), а не файлом целиком: докачка
# файла (bigfile), PDF счёта (invoicing), временный замерщик tg_probe (звук и
# свой журнал замеров, удаляется после перехода, BACKLOG).
ALLOWED = {
    ("src/claude_tg_bot/bigfile.py", "os.replace(part, target)"),
    ("src/claude_tg_bot/invoicing/render.py", "shutil.move(pdf, out)"),
    ("src/claude_tg_bot/tg_probe.py", "os.replace(tmp, path)"),
    ("src/claude_tg_bot/tg_probe.py", 'f.write(json.dumps(r, ensure_ascii=False) + "\\n")'),
}
STDIO = {"sys.stdout", "sys.stderr"}
SKIP_FILES = {"src/claude_tg_bot/jsonstore.py"}


def _sources() -> list[Path]:
    files = [*(ROOT / "src" / "claude_tg_bot").rglob("*.py"), *(ROOT / "tools").rglob("*.py"),
             ROOT / "bot-down-notify.py"]
    return [p for p in files if str(p.relative_to(ROOT)) not in SKIP_FILES]


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _has_dumps(nodes) -> bool:
    return any(isinstance(n, ast.Call) and _name(n.func) == "json.dumps"
               for node in nodes for n in ast.walk(node))


def _hand_writes(tree: ast.AST) -> list[ast.Call]:
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in ("os.replace", "os.rename", "os.link", "shutil.move", "json.dump"):
            bad.append(node)
        elif attr in ("replace", "rename") and (
                (len(node.args) == 1 and not node.keywords)
                or (not node.args and [k.arg for k in node.keywords] == ["target"])):
            # str.replace берёт два аргумента; с одним это Path.replace/rename.
            bad.append(node)
        elif (attr in ("write_text", "write", "write_bytes") and _name(node.func.value) not in STDIO
              and _has_dumps([*node.args, *node.keywords])):
            bad.append(node)
        elif name == "os.open" and any("O_APPEND" in _name(n) for a in node.args for n in ast.walk(a)):
            bad.append(node)
    return bad


def test_no_hand_written_state_files() -> None:
    bad = []
    for p in _sources():
        rel, text = str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")
        for call in _hand_writes(ast.parse(text)):
            if (rel, ast.get_source_segment(text, call)) not in ALLOWED:
                bad.append(f"{rel}:{call.lineno}")
    assert bad == [], "запись файлов состояния только через jsonstore (CLAUDE.md бота)"


def test_barrier_catches_known_forms() -> None:
    src = (
        "os.replace(a, b)\n"
        "tmp.replace(path)\n"
        "json.dump(x, f)\n"
        "p.write_text(json.dumps(x), encoding='utf-8')\n"
        "f.write(json.dumps(x) + '\\n')\n"
        "os.link(a, b)\n"
        "os.open(p, os.O_WRONLY | os.O_APPEND)\n"
        "p.rename(target=q)\n"
        "s.replace('a', 'b')\n"
        "p.write_text('x')\n"
        "json.dumps(x)\n"
        "os.open(p, os.O_RDONLY)\n"
        "sys.stdout.write(json.dumps(x))\n"
    )
    assert [c.lineno for c in _hand_writes(ast.parse(src))] == [1, 2, 3, 4, 5, 6, 7, 8]
