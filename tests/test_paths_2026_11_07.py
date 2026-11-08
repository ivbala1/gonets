"""Корень бота и пути от него (paths, ревизия повторов 07.11.2026, п. 5),
одно правило разбора настроек (config._bool_env, _int_env) и коды выхода
команд агента. Преграда: `parents[` вне paths.py и голый `load_dotenv()`."""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_tg_bot import config, notify, paths, restart_request, tasks  # noqa: E402
from claude_tg_bot.skills.chat_digest import __main__ as chat_digest  # noqa: E402
from claude_tg_bot.skills.news_digest import __main__ as news_digest  # noqa: E402
from claude_tg_bot.skills.task_new import __main__ as task_new  # noqa: E402

PKG = ROOT / "src" / "claude_tg_bot"


# ---- paths ----

def test_root_is_repo() -> None:
    assert paths.BOT_ROOT == ROOT
    assert paths.ENV_FILE == ROOT / ".env"
    assert paths.VENV_PYTHON == ROOT / ".venv" / "bin" / "python"


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_state_dir_default(raw, monkeypatch) -> None:
    if raw is None:
        monkeypatch.delenv("CLAUDE_TG_BOT_STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", raw)
    assert paths.state_dir() == ROOT / "state"


def test_state_dir_relative_is_from_root_not_cwd(monkeypatch, tmp_path) -> None:
    # `./state` из .env Ивана: команда агента, запущенная из другого
    # каталога, раньше заводила там свой state/.
    monkeypatch.chdir(tmp_path)
    for raw in ("./state", "state", "state/"):
        monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", raw)
        assert paths.state_dir() == ROOT / "state"
    monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", "../elsewhere")
    assert paths.state_dir() == (ROOT.parent / "elsewhere").resolve()


def test_state_dir_absolute_and_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", str(tmp_path))
    assert paths.state_dir() == tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", "~/st")
    assert paths.state_dir() == (tmp_path / "st").resolve()


def test_hook_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CLAUDE_HOOK_SETTINGS", raising=False)
    assert paths.hook_settings() == ROOT / "claude-hook-settings.json"
    monkeypatch.setenv("CLAUDE_HOOK_SETTINGS", str(tmp_path / "h.json"))
    assert paths.hook_settings() == tmp_path / "h.json"


def test_skills_follow_env_at_call_time(monkeypatch, tmp_path) -> None:
    """Скиллы читают каталог состояния и настройки хука после load_dotenv,
    то есть в момент вызова, а не при импорте."""
    monkeypatch.setenv("CLAUDE_TG_BOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOOK_SETTINGS", str(tmp_path / "h.json"))
    ask = tmp_path / "ask.py"
    ask.write_text("")
    monkeypatch.setattr(tasks, "two_mind_ask", lambda: ask)
    assert news_digest.state_root() == tmp_path / "news_digest"
    assert f"{tmp_path / '2mind'}/" in tasks.think_rules()
    lock = chat_digest._lock()
    try:
        assert lock is not None and Path(lock.name) == tmp_path / "chat_digest.lock"
    finally:
        lock.close()
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        raise OSError("stop")
    from claude_tg_bot.skills import _oneshot
    for call in (lambda: chat_digest.run_digest_claude("p", 5),
                 lambda: news_digest.run_claude("p", 5, "sonnet")):
        with patch.object(_oneshot, "run_claude_oneshot", fake_run), \
                patch.object(_oneshot, "resolve_claude_bin", return_value="claude"), pytest.raises(RuntimeError):
            call()
        args = seen.pop("args")
        assert args[args.index("--settings") + 1] == str(tmp_path / "h.json")


def test_config_uses_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    env = {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_ALLOWED_USER_IDS": "1", "CLAUDE_BIN": "/usr/bin/true",
           "CLAUDE_TG_BOT_STATE_DIR": "./state", "CLAUDE_HOOK_SETTINGS": "",
           "NEWS_TG_CHAT_ID": "-100", "NEWS_TG_THREAD_ID": "мусор"}
    with patch.object(config, "load_dotenv"), patch.dict("os.environ", env, clear=True), \
            patch("sys.stderr", io.StringIO()) as err:
        cfg = config.load_config(skip_hook_validation=True)
    assert cfg.state_dir == ROOT / "state"
    assert cfg.hook_settings_path == ROOT / "claude-hook-settings.json"
    assert (cfg.news_tg_chat_id, cfg.news_tg_thread_id) == (-100, None)
    assert "NEWS_TG_THREAD_ID" in err.getvalue()


# ---- настройки ----

@pytest.mark.parametrize("raw,default,want", [
    ("1", False, True), ("TRUE", False, True), (" yes ", False, True), ("On", False, True),
    ("0", True, False), ("false", True, False), ("NO", True, False), ("off", True, False),
    ("", True, True), ("", False, False), (None, True, True),
])
def test_bool_env(raw, default, want, monkeypatch) -> None:
    if raw is None:
        monkeypatch.delenv("X_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_FLAG", raw)
    assert config._bool_env("X_FLAG", default) is want


@pytest.mark.parametrize("default", [True, False])
def test_bool_env_garbage_is_default_with_warning(default, monkeypatch, capsys) -> None:
    monkeypatch.setenv("X_FLAG", "maybe")
    assert config._bool_env("X_FLAG", default) is default
    assert "X_FLAG" in capsys.readouterr().err


def test_digest_garbage_number_is_rc_not_traceback() -> None:
    """Мусор в номере темы сводки: код 1 с причиной, а не трейсбек."""
    for mod, env in ((chat_digest, {"CHAT_DIGEST_TG_CHAT_ID": "-100", "CHAT_DIGEST_TG_THREAD_ID": "x7"}),
                     (news_digest, {"NEWS_TG_CHAT_ID": "-100", "NEWS_TG_THREAD_ID": "x7"})):
        with patch.object(mod, "load_dotenv"), patch.dict("os.environ", env), \
                patch("sys.stderr", io.StringIO()) as err:
            assert mod.main([]) == 1
        assert "x7" in err.getvalue()


# ---- коды выхода команд агента ----

def test_notify_cli_bad_config_is_rc_1() -> None:
    with patch.object(notify, "load_config", side_effect=RuntimeError("TELEGRAM_BOT_TOKEN is empty")), \
            patch("sys.stderr", io.StringIO()) as err:
        assert notify.main(["текст"]) == 1
    assert "TELEGRAM_BOT_TOKEN is empty" in err.getvalue() and "Traceback" not in err.getvalue()


V4 = SimpleNamespace(approve_http_host="127.0.0.1", approve_http_port=8788, approve_shared_secret="",
                     state_dir=Path("/nonexistent"))


def _task_new(post) -> int:
    with patch.object(task_new, "load_config", return_value=V4), patch.object(task_new.httpx, "post", post), \
            patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
        return task_new.main(["слова задачи"])


def test_task_new_codes() -> None:
    ok = SimpleNamespace(status_code=200, json=lambda: {"text": "тема"}, text="")
    refused = SimpleNamespace(status_code=403, json=lambda: {"error": "нет"}, text="")
    broken = SimpleNamespace(status_code=502, json=lambda: {}, text="bad gateway")
    assert _task_new(MagicMock(return_value=ok)) == 0
    assert _task_new(MagicMock(return_value=refused)) == 2
    assert _task_new(MagicMock(return_value=broken)) == 3
    assert _task_new(MagicMock(side_effect=task_new.httpx.ReadTimeout("x"))) == 3
    with patch.object(task_new, "load_config", side_effect=RuntimeError("плохо")), \
            patch("sys.stderr", io.StringIO()) as err:
        assert task_new.main(["слова"]) == 2
    assert "плохо" in err.getvalue()


def test_mailsend_bad_config_is_rc_2() -> None:
    from claude_tg_bot import mailsend
    with patch.object(mailsend, "_load_cfg", side_effect=RuntimeError("плохо")), \
            patch("sys.stderr", io.StringIO()) as err:
        assert mailsend.main(["--to", "a@example.com", "--subject", "s", "--body-file", "/nonexistent"]) == 2
    assert "плохо" in err.getvalue()


def test_env_path_for_other_files(monkeypatch, tmp_path) -> None:
    """Пути к файлам из переменных скиллов и почты тоже от корня бота, `~` в умолчании раскрыт."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAILSEND_ENV", "x.env")
    assert paths.env_path("MAILSEND_ENV", ".env") == ROOT / "x.env"
    monkeypatch.delenv("MAILSEND_ENV")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.env_path("MAILSEND_ENV", "~/a/b.env") == tmp_path / "a" / "b.env"


def _restart(resp) -> int:
    with patch.object(restart_request, "git_state", return_value=("abc", "")), \
            patch.object(restart_request, "code_problem", return_value=""), \
            patch.object(restart_request, "load_config", return_value=V4), \
            patch.object(restart_request.httpx, "post", return_value=resp), \
            patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
        return restart_request.main(["--reason", "x"])


def test_restart_request_codes() -> None:
    def resp(status, body):
        def js():
            if body is None:
                raise ValueError("bad json")
            return body
        return SimpleNamespace(status_code=status, headers={"content-type": "application/json"},
                               json=js, text="")
    assert _restart(resp(200, {"id": "r1"})) == 0
    assert _restart(resp(409, {"error": "уже"})) == 2
    assert _restart(resp(502, None)) == 3
    assert _restart(resp(200, None)) == 3


def test_restart_request_bad_config_is_rc_2() -> None:
    with patch.object(restart_request, "git_state", return_value=("abc", "")), \
            patch.object(restart_request, "code_problem", return_value=""), \
            patch.object(restart_request, "load_config", side_effect=RuntimeError("плохо")), \
            patch("sys.stderr", io.StringIO()) as err:
        assert restart_request.main(["--reason", "x"]) == 2
    assert "плохо" in err.getvalue()


# ---- преграда ----

def _parents_subscripts(tree: ast.AST) -> list[int]:
    """`x.parents[N]` и `x.parent.parent` (корень руками, ревью 08.11.2026)."""
    return [n.lineno for n in ast.walk(tree)
            if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "parents")
            or (isinstance(n, ast.Attribute) and n.attr == "parent"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "parent")]


def _is_find_dotenv(node: ast.AST) -> bool:
    f = node.func if isinstance(node, ast.Call) else None
    return (isinstance(f, ast.Name) and f.id == "find_dotenv") or (
        isinstance(f, ast.Attribute) and f.attr == "find_dotenv")


def _bare_load_dotenv(tree: ast.AST) -> list[int]:
    """load_dotenv без пути (и с одними флагами вроде override=True) или с
    find_dotenv(): файл ищется от вызывающего, а не paths.ENV_FILE."""
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == "load_dotenv")
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == "load_dotenv"))
            and ((not n.args and not any(k.arg in ("dotenv_path", "stream") for k in n.keywords))
                 or (n.args and _is_find_dotenv(n.args[0]))
                 or any(k.arg == "dotenv_path" and _is_find_dotenv(k.value) for k in n.keywords))]


def test_no_hand_computed_root_or_bare_dotenv() -> None:
    """Корень бота считается только в paths.py; `.env` грузится явным путём
    (голый load_dotenv() ищет файл от вызывающего модуля или cwd)."""
    bad = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT)
        if path.name != "paths.py":
            bad += [f"{rel}:{n} parents[" for n in _parents_subscripts(tree)]
        bad += [f"{rel}:{n} load_dotenv()" for n in _bare_load_dotenv(tree)]
    assert bad == []


def test_barrier_catches_samples() -> None:
    tree = ast.parse("from pathlib import Path\nfrom dotenv import load_dotenv\nimport dotenv\n"
                     "A = Path(__file__).resolve().parents[2]\nB = A.parents[0] / 'x'\n"
                     "load_dotenv()\ndotenv.load_dotenv()\nload_dotenv(A / '.env')\n"
                     "load_dotenv(override=True)\nload_dotenv(dotenv.find_dotenv())\n"
                     "C = Path(__file__).resolve().parent.parent\nload_dotenv(A / '.env', override=True)\n")
    assert sorted(_parents_subscripts(tree)) == [4, 5, 11]
    assert sorted(_bare_load_dotenv(tree)) == [6, 7, 9, 10]
