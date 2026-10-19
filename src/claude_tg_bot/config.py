from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values, load_dotenv

# Снимок os.environ перед первым load_dotenv (см. load_config).
ENV_BEFORE_DOTENV: dict[str, str] | None = None

# Секреты бота, которые не должны попадать в окружение агента (claude, codex).
# load_dotenv кладёт весь .env в os.environ процесса бота, а дочерний claude
# раньше наследовал его копией: агент с Bash видел токен бота, секрет
# /approve и ключи API в `env`. Хук подтверждения и скиллы (notify, invoicing,
# таймерные) читают .env сами (load_dotenv от своего файла), окружение им не нужно.
BOT_SECRET_ENV_NAMES = frozenset({
    "TELEGRAM_BOT_TOKEN", "APPROVE_SHARED_SECRET", "OPENAI_API_KEY",
    "IMMICH_API_KEY", "NEWS_GITHUB_TOKEN", "GEMINI_API_KEY", "TELEGRAM_API_HASH",
})
# Новые секреты в .env бота ловятся по имени, без правки списка выше.
_SECRET_NAME_RE = re.compile(r"(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)$")
# MCP на чтение без карточки (APPROVE_MCP_AUTO_ALLOW_REGEX), см. load_config.
DEFAULT_MCP_AUTO_ALLOW_REGEX = (
    r"(?!get_invite_link$)(?:slack_)?(?:analyze|fetch|get|list|lookup|query|read|search|suggest|validate)[A-Za-z0-9_]*"
)
BOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def agent_env(base: dict[str, str] | None = None, *, env_file: Path = BOT_ENV_FILE) -> dict[str, str]:
    """Окружение для дочернего агента: копия base (по умолчанию os.environ)
    без секретов бота. Из .env берутся только имена ключей."""
    env = dict(os.environ if base is None else base)
    names = set(BOT_SECRET_ENV_NAMES)
    try:
        names.update(k for k in dotenv_values(env_file) if _SECRET_NAME_RE.search(k))
    except OSError:
        pass
    for name in names:
        env.pop(name, None)
    return env

# Ожидаемые инструменты, которые ДОЛЖНЫ проходить через approve-хук.
# Если matcher из settings не покрывает какой-то из них — это означает,
# что bypassPermissions превращается в прямой bypass для этого инструмента.
REQUIRED_APPROVE_TOOLS = (
    "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch",
    # Monitor запускает shell-команду в фоне, как Bash (есть в tools у
    # claude -p 2.1.292, проверено 07.10.2026).
    "Monitor",
    # Синтетическое имя: matcher обязан покрывать mcp__* — иначе под
    # bypassPermissions любой MCP-инструмент (Slack/Gmail/Jira/Grafana...)
    # выполняется вообще без approve-карточки.
    "mcp__example-server__example_tool",
)
HOOK_APPROVE_MODULE = "claude_tg_bot.hook_approve"
TZ_ENV = "CLAUDE_TG_BOT_TZ"


def _system_zone_name() -> str:
    """Имя часового пояса системы (Europe/Berlin) или пустая строка."""
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if "/" in tz:
        return tz
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def local_zone() -> ZoneInfo:
    """Часовой пояс расписаний бота: CLAUDE_TG_BOT_TZ, иначе пояс системы, иначе UTC."""
    name = os.environ.get(TZ_ENV, "").strip() or _system_zone_name()
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _validate_hook_settings(path: Path) -> None:
    """Fail-closed проверка PreToolUse-хука. Вызывается на старте.

    Если settings-файл отсутствует или не содержит подключения нашего hook_approve
    с правильным matcher'ом — bypassPermissions становится прямым bypass для
    destructive tools. Лучше отказаться стартовать, чем тихо выдать Claude полный доступ.
    """
    if not path.exists():
        raise RuntimeError(
            f"hook settings file not found: {path}. "
            "Approve-механизм не подключится — отказ стартовать (fail-closed)."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"hook settings file invalid: {path}: {e}") from e

    pre_tool = (data.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pre_tool, list) or not pre_tool:
        raise RuntimeError(f"{path}: hooks.PreToolUse is empty or not a list")

    # Считаем покрытие ТОЛЬКО по entries, которые реально вызывают наш hook_approve.
    # Иначе конфиг с "Bash|Write|…" в одном entry и hook_approve в другом (на других
    # tools) ложно пройдёт проверку, а approve на Bash/Write не сработает.
    def _entry_calls_approve(entry: dict) -> bool:
        for h in (entry.get("hooks") or []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command")
            if isinstance(cmd, str) and HOOK_APPROVE_MODULE in cmd:
                return True
        return False

    matchers_with_approve: list[str] = []
    for entry in pre_tool:
        if not isinstance(entry, dict):
            continue
        m = entry.get("matcher")
        if _entry_calls_approve(entry) and isinstance(m, str):
            matchers_with_approve.append(m)

    if not matchers_with_approve:
        raise RuntimeError(
            f"{path}: ни один PreToolUse entry не зовёт {HOOK_APPROVE_MODULE!r}. "
            "Approve-мост не активен — отказ стартовать."
        )

    # Компилируем regex-matcher'ы. Claude Code сравнивает имя tool'а с matcher'ом
    # через regex — делаем то же самое, иначе substring-проверка ("Bash" in "NoBashHere")
    # пропустит конфиг, который реально ничего не матчит.
    compiled: list[tuple[str, re.Pattern]] = []
    for m in matchers_with_approve:
        try:
            compiled.append((m, re.compile(m)))
        except re.error as e:
            raise RuntimeError(f"{path}: невалидный regex в matcher {m!r}: {e}") from e

    def _tool_matched(tool: str) -> bool:
        return any(pat.fullmatch(tool) for _, pat in compiled)

    missing = [t for t in REQUIRED_APPROVE_TOOLS if not _tool_matched(t)]
    if missing:
        raise RuntimeError(
            f"{path}: в PreToolUse-entries с {HOOK_APPROVE_MODULE!r} matcher-regex не матчат: {missing}. "
            f"Текущие matcher'ы: {matchers_with_approve}. "
            f"Ожидается regex, покрывающий {list(REQUIRED_APPROVE_TOOLS)} (например 'Bash|Monitor|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*')."
        )

    # Бот отвечает на карточку за 60 с до того, как CLI снимет хук: при
    # timeout 60 с и меньше ждать клика некогда (решение владельца 14.10.2026).
    short = [t for t in _approve_hook_timeouts(pre_tool) if t <= 60]
    if short:
        raise RuntimeError(
            f"{path}: timeout хука {HOOK_APPROVE_MODULE} {min(short)} с, нужно больше 60 с: "
            "бот отвечает на карточку за 60 с до того, как CLI снимет хук. "
            "Подними timeout (штатно 960). Отказ стартовать."
        )

    # Хук, который не запустился, упал до ответа (ошибка импорта, нет
    # интерпретатора) или снят по таймауту, без onFailure block пропускает
    # действие, под bypassPermissions без всякой проверки (проверено на
    # 2.1.303 19.10.2026, решение владельца 19.10.2026).
    if any(h.get("onFailure") != "block" for h in _approve_hooks(pre_tool)):
        raise RuntimeError(
            f'{path}: у хука {HOOK_APPROVE_MODULE} нет "onFailure": "block": сбой или '
            "таймаут хука пропустил бы действие без проверки. Добавь ключ (Claude Code "
            "2.1.295 и новее). Отказ стартовать."
        )


def _approve_hooks(pre_tool: object) -> list[dict]:
    """Хуки PreToolUse, которые зовут hook_approve."""
    hooks: list[dict] = []
    for entry in pre_tool if isinstance(pre_tool, list) else []:
        for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            if isinstance(h, dict) and HOOK_APPROVE_MODULE in str(h.get("command", "")):
                hooks.append(h)
    return hooks


def _approve_hook_timeouts(pre_tool: object) -> list[float]:
    """Числовые `timeout` хуков, которые зовут hook_approve; булево не число."""
    timeouts: list[float] = []
    for h in _approve_hooks(pre_tool):
        timeout = h.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            timeouts.append(timeout)
    return timeouts


def _cap_approve_timeout(path: Path, approve_timeout: int, *, warn: bool = True) -> int:
    """CLI снимает хук по его `timeout` из settings, а hook_approve ждёт бота
    APPROVE_TIMEOUT_SEC + 30 с. Если хук снимут раньше, поздний клик ни на что
    не влияет, а исход решает CLI, а не бот (ревью 11.10.2026). Поэтому
    ожидание карточки урезается до наименьшего timeout хука минус 60 с: бот
    всегда отвечает раньше, чем CLI снимет хук. Урезание действует и в
    процессе хука, который настройки не проверяет (решение владельца 12.10.2026)."""
    try:
        pre_tool = (json.loads(path.read_text()).get("hooks") or {}).get("PreToolUse") or []
    except (OSError, ValueError, AttributeError):
        return approve_timeout
    timeouts = _approve_hook_timeouts(pre_tool)
    if not timeouts:
        return approve_timeout
    # Не меньше 1 с: timeout хука 60 с и меньше отклоняет проверка настроек,
    # а процесс хука её не делает (решение владельца 14.10.2026).
    shortest = int(min(timeouts))
    limit = max(1, shortest - 60)
    if approve_timeout <= limit:
        return approve_timeout
    if warn:
        print(
            f"[config] WARNING: APPROVE_TIMEOUT_SEC={approve_timeout} урезан до {limit}: "
            f"timeout хука в {path} {shortest} с, бот должен ответить раньше, чем CLI "
            "снимет хук. Для более долгого ожидания подними timeout хука.",
            file=sys.stderr,
        )
    return limit


# Допустимые значения `--effort` для claude (CLI 2.x). Порядок = от меньшего
# к большему усилию; используется и в валидации конфига, и командой /effort.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# Кнопки /model, когда CLAUDE_MODEL_CHOICES пуст: алиасы семейств. Свежую
# модель по алиасу подставляет сам CLI, поэтому список не устаревает.
DEFAULT_MODEL_CHOICES = ("fable", "opus", "sonnet", "haiku")


@dataclass(frozen=True)
class Project:
    """Проект, к которому привязывается forum-тема по имени (авто-по-имени).

    match — подстроки (lowercase), которые ищутся в имени темы Telegram.
    append_prompt — готовый блок, который бот добавляет к --append-system-prompt
        для сообщений из этой темы (фрейминг + указание на индекс-доки/репо).
    """
    key: str
    match: tuple[str, ...]
    append_prompt: str


def _synthesize_project_prompt(key: str, spec: dict) -> str:
    """Собрать system-prompt блок из docs/repos, если явный 'prompt' не задан."""
    name = str(spec.get("name") or key)
    docs = [str(d).strip() for d in (spec.get("docs") or []) if str(d).strip()]
    repos = [str(r).strip() for r in (spec.get("repos") or []) if str(r).strip()]
    lines = [
        f"Этот разговор идёт в Telegram-теме проекта «{name}». Вопросы могут "
        "касаться любой части проекта (архитектура, поддержка, регламенты, инфра "
        "и т.п.) и меняться со временем — не сужай контекст под одну подтему."
    ]
    if docs:
        lines.append(
            "Точка входа в контекст проекта (карта/индекс) — начни отсюда и "
            "дочитай под конкретный вопрос то, что релевантно:"
        )
        lines += [f"- {d}" for d in docs]
    if repos:
        lines.append("Релевантные репозитории проекта:")
        lines += [f"- {r}" for r in repos]
    lines.append(
        f"Держись в рамках проекта «{name}»: не трогай другие проекты и репозитории, "
        "если пользователь явно не попросил об обратном."
    )
    return "\n".join(lines)


def load_projects(path: Path) -> tuple[Project, ...]:
    """Загрузить мапу проектов из projects.json. Фича опциональна (auto-scoping
    тем по имени) — при отсутствии/битом файле возвращаем (), не падая: худший
    случай — просто нет скоупинга, бот работает как раньше.

    Формат:
        {"alpha": {"name": "Альфа", "match": ["альфа","alpha"],
                    "docs": ["/abs/CLAUDE.md"], "repos": ["/abs/repo"]}}
    Поле "prompt" (строка) переопределяет авто-сборку из docs/repos.
    """
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[config] WARNING: projects file {path} невалиден: {e} — скоупинг тем выключен", file=sys.stderr)
        return ()
    if not isinstance(data, dict):
        print(f"[config] WARNING: projects file {path} не JSON-объект — скоупинг тем выключен", file=sys.stderr)
        return ()
    projects: list[Project] = []
    for key, spec in data.items():
        if key.startswith("_"):
            continue  # ключи на "_" — JSON-комментарии (напр. "_comment"), молча пропускаем
        if not isinstance(spec, dict):
            print(f"[config] WARNING: project {key!r} пропущен: значение не объект", file=sys.stderr)
            continue
        raw_match = spec.get("match") or []
        if isinstance(raw_match, str):
            raw_match = [raw_match]
        match = tuple(str(m).strip().lower() for m in raw_match if str(m).strip())
        if not match:
            print(f"[config] WARNING: project {key!r} пропущен: пустой 'match'", file=sys.stderr)
            continue
        prompt = spec.get("prompt")
        if not (isinstance(prompt, str) and prompt.strip()):
            prompt = _synthesize_project_prompt(key, spec)
        projects.append(Project(key=str(key), match=match, append_prompt=prompt.strip()))
    return tuple(projects)


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]  # пусто = только private chats whitelisted user'ов
    claude_bin: str
    claude_cwd: Path
    hook_settings_path: Path
    state_dir: Path
    approve_whitelist_paths: frozenset[Path]
    # Чаты с автоаппрувом по умолчанию: любой tool проходит без карточки, и в
    # обычных, и в unattended-ранах (события по таймеру в этот чат). Ключи в
    # формате chat_key: "<chat_id>" или "<chat_id>:<thread_id>". Пусто = выкл.
    approve_auto_chat_keys: frozenset[str]
    log_level: str
    approve_http_host: str
    approve_http_port: int
    approve_timeout_sec: int
    approve_shared_secret: str
    openai_api_key: str
    openai_tts_voice: str
    openai_stt_model: str
    # Контекст для STT: prompt — описание тематики, keywords — буквальные
    # термины (только gpt-transcribe), languages — подсказки языков
    # (gpt-transcribe: список; whisper/4o: берётся первый как language).
    openai_stt_prompt: str
    openai_stt_keywords: tuple[str, ...]
    openai_stt_languages: tuple[str, ...]
    openai_tts_model: str
    openai_tts_instructions: str
    voice_reply_max_chars: int
    voice_max_duration_sec: int
    telegram_text_limit: int
    telegram_caption_limit: int
    large_reply_as_file_threshold: int
    # Persona-текст для --append-system-prompt; пусто = без persona.
    # Читается один раз при старте (правка файла требует рестарта бота).
    persona_text: str
    # Жёсткий потолок времени работы одного `claude -p` под чатом. Защита от
    # бесконечного зацикливания/сетевых висяков, при которых /cancel не пришёл.
    # 0 = без таймаута (старое поведение).
    max_claude_runtime_sec: int
    # TTL дней для файлов в state/attachments/. 0 = не чистить.
    attachment_ttl_days: int
    # Максимальный размер входящего фото/image-document, байты. Telegram сам
    # режет на 50MB, но Claude через Read большие изображения может тяжело
    # прожевать. Рекомендуется ≤10MB.
    photo_max_size_bytes: int
    # Максимальный размер входящего не-image документа (pdf/docx/zip/...), байты.
    # Отдельно от фото: документация обычно крупнее изображений. 0 = без лимита.
    document_max_size_bytes: int
    # Вложения больше 20 МБ (предел getFile Bot API) бот качает через MTProto,
    # войдя ботом (bigfile.py). Ключи приложения с my.telegram.org; 0 или
    # пустой hash = большие файлы выключены с понятным отказом.
    telegram_api_id: int
    telegram_api_hash: str
    # Свободного места после большой загрузки должно остаться не меньше.
    download_min_free_bytes: int
    # Общий срок одной большой загрузки (вход, повтор, скачивание), секунды.
    big_download_timeout_sec: int
    # Regex для auto-allow безопасных Bash-команд без approve-карточки.
    # Каждый сегмент команды (split по |/;/&&/||) должен матчиться + не содержать
    # `$(` `` ` `` `>` `<` (за исключением безобидных `2>/dev/null`/`2>&1` etc).
    # None = выключено (старое поведение, все Bash через approve).
    bash_auto_allow_re: re.Pattern[str] | None
    # Regex для auto-allow read-only MCP-инструментов без approve-карточки.
    # Матчится по имени инструмента БЕЗ префикса mcp__<server>__ (fullmatch).
    # Нужен, чтобы mcp__* в hook-matcher не душил карточками ни чтение из
    # чата, ни unattended-скиллы (Gmail/Slack-чекеры по таймеру). Мутирующие
    # MCP (send/create/delete/update...) обязаны идти через approve.
    # None = все MCP через approve.
    mcp_auto_allow_re: re.Pattern[str] | None
    # Дефолтный effort-уровень, который бот передаёт claude через `--effort`
    # на каждый run. Per-chat override крутится в рантайме командой /effort.
    # Своё имя (не CLAUDE_CODE_EFFORT_LEVEL): та env-переменная перебивает флаг
    # `--effort`, утекла бы в субпроцесс и сломала бы runtime-переключение.
    effort_level: str
    # Проекты для авто-скоупинга forum-тем по имени. Пусто = фича выключена.
    projects: tuple[Project, ...]
    # Idle-TTL сессии (сек): если в теме не писали дольше — новый запрос
    # стартует свежую сессию (не --resume), чтобы не переигрывать всю историю
    # в холодный prompt-cache. 0 = выключено (всегда resume, старое поведение).
    session_idle_ttl_sec: int
    # Модель для claude (--model). Пусто = дефолт CLI. Per-chat override — /model.
    # Зачем явно: дефолт CLI может указывать на недоступную аккаунту модель (404).
    model: str
    # Варианты для кнопок /model: CLAUDE_MODEL_CHOICES (comma-separated в .env)
    # либо DEFAULT_MODEL_CHOICES. На дефолт не влияет, дефолт задаёт `model`.
    model_choices: tuple[str, ...]
    # True, когда CLAUDE_MODEL_CHOICES пуст: кнопки это алиасы семейств, а
    # models.py подписывает их новейшей моделью по живому списку API.
    model_choices_dynamic: bool
    # Показывать ли футер со стоимостью/размером контекста после ответа.
    show_cost_footer: bool
    # Порог контекста (токены), выше которого футер предупреждает про /reset. 0=выкл.
    context_warn_tokens: int
    # --- Codex CLI (OpenAI) как фоллбэк/альтернатива claude ---
    # Абсолютный путь к НАТИВНОМУ Linux-бинарю codex (шим из PATH — Windows-npm —
    # из Linux-cwd падает на UNC). Пусто = вся codex-интеграция выключена.
    codex_bin: str
    # Авто-переключение на codex, когда claude вернул ошибку доступности
    # (401/429/5xx). Действует только если codex_bin задан.
    codex_fallback: bool
    # Потолок времени одного `codex exec`, сек.
    codex_timeout_sec: int
    # Sandbox-политика codex для РУЧНОГО режима (gpt:-префикс) — явный выбор юзера.
    codex_sandbox: str
    # Sandbox для АВТО-фоллбэка (claude недоступен): дефолт read-only, чтобы сбой
    # claude не давал обычному запросу неожиданный write-доступ к workspace.
    codex_fallback_sandbox: str
    # Сетевой доступ codex внутри sandbox (Codex CLI: sandbox_workspace_write.
    # network_access). Действует только при workspace-write/danger-full-access —
    # read-only держит сеть закрытой всегда, отдельного тумблера для него нет.
    # ⚠️ У codex НЕТ approve-карточек бота (своя sandbox-модель), поэтому сеть
    # здесь означает право сделать мутирующий HTTP-запрос без подтверждения.
    codex_network_access: bool
    # Префикс ручного триггера: сообщение, начинающееся с него, уходит в codex.
    codex_trigger_prefix: str
    # Сторож тишины рана claude (сек): нет ни одного события stream-json дольше
    # этого, и бот шлёт тревогу со звуком и кнопками «Ждать»/«Прервать».
    # 0 = выкл. Дефолты здесь, а не только в load_config: тесты собирают cfg
    # без этих полей.
    run_silence_alert_sec: int = 600
    # Через сколько секунд работы рана один раз прислать «Работаю N минут». 0 = выкл.
    run_progress_note_sec: int = 900
    # Окно автосжатия claude (--autocompact): "" = не передавать (дефолт CLI),
    # "auto" или "300k" и т.п. (100k-1M). autocompact_chats: ключи тем, где
    # действует; пусто = во всех.
    autocompact: str = ""
    autocompact_chats: frozenset[str] = frozenset()
    @property
    def primary_chat_id(self) -> int:
        """Стабильно (min) выбранный user_id для сервисных уведомлений/approvals."""
        return min(self.allowed_user_ids)


def _int_env(name: str, default: int) -> int:
    """int из env с fallback: пустое/мусор → default + WARNING, а не падение
    старта трейсбэком (опечатка в одном числовом ключе не должна валить бот)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] WARNING: {name}={raw!r} не целое число — беру {default}", file=sys.stderr)
        return default


_AUTOCOMPACT_RE = re.compile(r"(\d+(?:\.\d+)?)([km]?)")


def _autocompact_env(raw: str) -> str:
    """CLAUDE_AUTOCOMPACT: "auto" или размер окна 100k-1M (как у --autocompact
    в CLI 2.1.296). Неверное значение: старт падает, а не молча без сжатия."""
    value = raw.strip().lower()
    if not value or value == "auto":
        return value
    m = _AUTOCOMPACT_RE.fullmatch(value)
    if m:
        tokens = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
        if 100_000 <= tokens <= 1_000_000:
            return value
    raise RuntimeError(f"CLAUDE_AUTOCOMPACT={raw!r}: нужно auto или размер 100k-1M, например 300k")


def load_config(*, skip_hook_validation: bool = False) -> Config:
    """Загрузить и провалидировать конфиг.

    skip_hook_validation=True использует hook_approve.py, который сам живёт
    внутри hook-процесса — там повторно валидировать settings смысла нет,
    а падение с трейсбэком вместо JSON-ответа сломало бы approve-протокол.
    """
    # Явный путь к .env бота: хук и скиллы, запущенные агентом, получают
    # окружение без секретов (agent_env) и обязаны найти .env сами из любой cwd.
    # Окружение до .env: с ним проверка перезапуска (restart.fresh_env)
    # повторяет свежий старт сервиса. Только при первой загрузке процесса.
    global ENV_BEFORE_DOTENV
    if ENV_BEFORE_DOTENV is None:
        ENV_BEFORE_DOTENV = dict(os.environ)
    # Голый load_dotenv() при `python -c` искал бы от cwd, а не от этого файла.
    load_dotenv(BOT_ENV_FILE if BOT_ENV_FILE.is_file() else None)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

    raw_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw_ids:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS is empty")
    try:
        allowed = frozenset(int(x) for x in raw_ids.split(",") if x.strip())
    except ValueError as e:
        raise RuntimeError(f"TELEGRAM_ALLOWED_USER_IDS содержит не-число: {e}") from e
    # Пустой после фильтрации (например ' , ') прошёл бы проверку выше, но дал бы
    # пустой whitelist → primary_chat_id (min() пустого) упал бы позже. Fail-fast.
    if not allowed:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS не содержит ни одного id")

    raw_chats = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    try:
        allowed_chats = frozenset(int(x) for x in raw_chats.split(",") if x.strip()) if raw_chats else frozenset()
    except ValueError as e:
        raise RuntimeError(f"TELEGRAM_ALLOWED_CHAT_IDS содержит не-число: {e}") from e

    claude_bin = os.environ.get("CLAUDE_BIN", "").strip() or "claude"
    if not os.path.isabs(claude_bin):
        # Под systemd --user PATH не наследуется от shell, поэтому relative path
        # почти гарантированно сломает запуск. Предупредим заранее, не падая.
        resolved = shutil.which(claude_bin)
        if resolved is None:
            print(
                f"[config] WARNING: CLAUDE_BIN={claude_bin!r} не абсолютный путь "
                "и не найден в PATH текущего процесса. Под systemd будет FileNotFoundError. "
                "Рекомендуется прописать абсолютный путь в .env.",
                file=sys.stderr,
            )
    # Без CLAUDE_CWD агент работает из домашнего каталога.
    claude_cwd = Path(
        os.environ.get("CLAUDE_CWD", "~")
    ).expanduser().resolve()

    # По умолчанию — рядом с пакетом: .../claude-tg-bot/claude-hook-settings.json
    default_hook_settings = Path(__file__).resolve().parents[2] / "claude-hook-settings.json"
    hook_settings = Path(
        os.environ.get("CLAUDE_HOOK_SETTINGS", str(default_hook_settings))
    ).expanduser().resolve()

    # Дефолт резолвим относительно project root (где pyproject.toml), а не cwd.
    # Иначе hook, запущенный claude в CLAUDE_CWD, и notify из произвольной cwd
    # создавали бы лишние state/ где угодно.
    default_state_dir = Path(__file__).resolve().parents[2] / "state"
    state_dir = Path(
        os.environ.get("CLAUDE_TG_BOT_STATE_DIR", str(default_state_dir))
    ).expanduser().resolve()
    # Не создаём директорию здесь — load_config зовётся и из hook_approve / notify,
    # которым state/ не нужен. SessionStore/audit сами делают mkdir(parents=True).

    # Path-whitelist: список АБСОЛЮТНЫХ каталогов (через `:`), внутри которых
    # Write/Edit/MultiEdit/NotebookEdit auto-approved без Telegram-кнопки.
    # Назначение: снизить approve-spam для низко-рискованных зон (memory/, scratch/).
    # Пусто = отключено.
    raw_wl = os.environ.get("APPROVE_WHITELIST_PATHS", "").strip()
    wl_paths: list[Path] = []
    for p in raw_wl.split(":"):
        p = p.strip()
        if not p:
            continue
        # Проверять `is_absolute()` нужно ДО `resolve()`. После resolve()
        # любой путь становится абсолютным (резолвится от os.getcwd() бота),
        # поэтому пост-resolve проверка ничего не отсеивает: relative
        # `state/` безмолвно превращался в `<bot-cwd>/state/` и попадал
        # в whitelist, хотя .env.example/README обещают список абсолютных.
        expanded = Path(p).expanduser()
        if not expanded.is_absolute():
            print(
                f"[config] WARNING: APPROVE_WHITELIST_PATHS skip {p!r}: not absolute "
                "(после expanduser). whitelist принимает только абсолютные пути.",
                file=sys.stderr,
            )
            continue
        try:
            resolved = expanded.resolve()
        except OSError as e:
            print(f"[config] WARNING: APPROVE_WHITELIST_PATHS skip {p!r}: {e}", file=sys.stderr)
            continue
        wl_paths.append(resolved)
    approve_whitelist_paths = frozenset(wl_paths)

    # Автоаппрув по чатам: решение Ивана 14.09.2026 для личного диалога
    # тренера, где карточки на запись журнала только мешали.
    approve_auto_chat_keys = frozenset(
        k.strip() for k in os.environ.get("APPROVE_AUTO_CHAT_KEYS", "").split(",") if k.strip()
    )

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # `or "127.0.0.1"` страхует случай APPROVE_HTTP_HOST= (присутствует, но пуст):
    # default срабатывает только если ключа нет; пустая строка прошла бы дальше
    # и aiohttp TCPSite слушал бы на all-interfaces без secret — fail-open.
    approve_host = os.environ.get("APPROVE_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    approve_port = _int_env("APPROVE_HTTP_PORT", 8788)
    approve_timeout = _int_env("APPROVE_TIMEOUT_SEC", 900)
    approve_secret = os.environ.get("APPROVE_SHARED_SECRET", "").strip()

    # Fail-closed: non-loopback bind без secret = кто угодно на сети может
    # дёргать /approve и спамить Telegram или забивать registry.
    if approve_host not in LOOPBACK_HOSTS and not approve_secret:
        raise RuntimeError(
            f"APPROVE_HTTP_HOST={approve_host!r} не loopback, но APPROVE_SHARED_SECRET пуст. "
            "Non-loopback bind требует обязательного secret — отказ стартовать."
        )

    # Валидируем hook-файл прямо тут, до запуска бота. Если что-то не так —
    # лучше RuntimeError при старте, чем тихий bypass в работе.
    if not skip_hook_validation:
        _validate_hook_settings(hook_settings)
    approve_timeout = _cap_approve_timeout(hook_settings, approve_timeout,
                                           warn=not skip_hook_validation)

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_voice = os.environ.get("OPENAI_TTS_VOICE", "nova").strip() or "nova"
    openai_stt = os.environ.get("OPENAI_STT_MODEL", "gpt-transcribe").strip() or "gpt-transcribe"
    openai_stt_prompt = os.environ.get("OPENAI_STT_PROMPT", "").strip()
    # Comma-separated. Пустое значение переменной = выключить подсказку.
    openai_stt_keywords = tuple(
        s for s in (x.strip() for x in os.environ.get("OPENAI_STT_KEYWORDS", "").split(",")) if s
    )
    openai_stt_languages = tuple(
        s for s in (x.strip() for x in os.environ.get("OPENAI_STT_LANGUAGES", "").split(",")) if s
    )
    openai_tts = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip() or "gpt-4o-mini-tts"
    # Поддерживается только моделью gpt-4o-mini-tts (steerability). Для tts-1/tts-1-hd
    # параметр игнорируется API. Пусто = не передавать вообще.
    openai_tts_instr = os.environ.get("OPENAI_TTS_INSTRUCTIONS", "").strip()
    voice_max = _int_env("VOICE_REPLY_MAX_CHARS", 4000)
    voice_dur = _int_env("VOICE_MAX_DURATION_SEC", 180)
    tg_text = _int_env("TELEGRAM_TEXT_LIMIT", 4000)
    tg_caption = _int_env("TELEGRAM_CAPTION_LIMIT", 1024)
    # Нулевые лимиты зацикливают разбиение ответа (ревью 12.10.2026).
    for name, value, ceiling in (("TELEGRAM_TEXT_LIMIT", tg_text, 4096),
                                 ("TELEGRAM_CAPTION_LIMIT", tg_caption, 1024),
                                 ("VOICE_REPLY_MAX_CHARS", voice_max, None)):
        if value < 1 or (ceiling is not None and value > ceiling):
            raise RuntimeError(f"{name}: недопустимый лимит {value}")
    large_as_file = _int_env("LARGE_REPLY_AS_FILE_THRESHOLD", 12000)
    # Default 3600s = 1 час. Покрывает большинство интерактивных задач без
    # риска зомби-процессов. Под batch-операции (обновление ОС серверов, dbRestore,
    # ansible apply на нескольких хостах) переопределяй через .env:
    #   MAX_CLAUDE_RUNTIME_SEC=5400  # 1.5 часа
    # Нулевое значение = без таймаута (опасно — зомби могут копиться).
    max_runtime = _int_env("MAX_CLAUDE_RUNTIME_SEC", 3600)
    # Сторож тишины и разовая весточка о долгом ране (см. bot._RunStatus).
    run_silence_alert = max(0, _int_env("RUN_SILENCE_ALERT_SEC", 600))
    run_progress_note = max(0, _int_env("RUN_PROGRESS_NOTE_SEC", 900))
    autocompact = _autocompact_env(os.environ.get("CLAUDE_AUTOCOMPACT", ""))
    autocompact_chats = frozenset(
        k.strip() for k in os.environ.get("CLAUDE_AUTOCOMPACT_CHATS", "").split(",") if k.strip()
    )
    attach_ttl = _int_env("ATTACHMENT_TTL_DAYS", 30)
    photo_max = _int_env("PHOTO_MAX_SIZE_BYTES", 10485760)
    document_max = _int_env("DOCUMENT_MAX_SIZE_BYTES", 524288000)
    tg_api_id = _int_env("TELEGRAM_API_ID", 0)
    tg_api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    min_free = _int_env("DOWNLOAD_MIN_FREE_BYTES", 2 * 1024 ** 3)
    big_timeout = _int_env("BIG_DOWNLOAD_TIMEOUT_SEC", 1800)

    effort_level = os.environ.get("CLAUDE_TG_BOT_EFFORT", "high").strip().lower() or "high"
    if effort_level not in EFFORT_LEVELS:
        raise RuntimeError(
            f"CLAUDE_TG_BOT_EFFORT={effort_level!r} invalid, допустимо: {', '.join(EFFORT_LEVELS)}"
        )

    raw_bash_re = os.environ.get("APPROVE_BASH_AUTO_ALLOW_REGEX", "").strip()
    bash_auto_allow_re: re.Pattern[str] | None = None
    if raw_bash_re:
        try:
            bash_auto_allow_re = re.compile(raw_bash_re)
        except re.error as e:
            raise RuntimeError(f"APPROVE_BASH_AUTO_ALLOW_REGEX invalid regex: {e}") from e

    # Дефолт покрывает read-only глаголы (в т.ч. camelCase вроде getJiraIssue
    # и slack_read_*/slack_search_*). Пустое значение переменной = выключить
    # auto-allow, все MCP через карточку.
    # download пишет файлы, get_invite_link создаёт ссылку (ревью 12.10.2026).
    raw_mcp_re = os.environ.get("APPROVE_MCP_AUTO_ALLOW_REGEX", DEFAULT_MCP_AUTO_ALLOW_REGEX).strip()
    mcp_auto_allow_re: re.Pattern[str] | None = None
    if raw_mcp_re:
        try:
            mcp_auto_allow_re = re.compile(raw_mcp_re)
        except re.error as e:
            raise RuntimeError(f"APPROVE_MCP_AUTO_ALLOW_REGEX invalid regex: {e}") from e

    # Persona: добавляется к системному промпту Claude на каждом run.
    # Дефолт — <project_root>/telegram-persona.md. Если файла нет — пусто (no-op).
    default_persona = Path(__file__).resolve().parents[2] / "telegram-persona.md"
    persona_file = Path(
        os.environ.get("CLAUDE_TG_BOT_PERSONA_FILE", str(default_persona))
    ).expanduser().resolve()
    persona_text = ""
    if persona_file.exists():
        try:
            persona_text = persona_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"[config] WARNING: не удалось прочитать persona {persona_file}: {e}", file=sys.stderr)

    # Проекты для авто-скоупинга forum-тем по имени. Дефолт — projects.json в
    # корне проекта (рядом с pyproject.toml). Файл env-специфичен (абсолютные
    # пути на сервере), поэтому в репо лежит projects.example.json.
    default_projects = Path(__file__).resolve().parents[2] / "projects.json"
    projects_file = Path(
        os.environ.get("CLAUDE_TG_BOT_PROJECTS_FILE", str(default_projects))
    ).expanduser().resolve()
    projects = load_projects(projects_file)

    # Idle-TTL сессии в часах (env), храним в секундах. 0/отрицательное = выкл.
    # float, чтобы можно было 1.5 и т.п. Дефолт 30ч.
    try:
        ttl_hours = float(os.environ.get("SESSION_IDLE_TTL_HOURS", "30") or "30")
    except ValueError:
        print("[config] WARNING: SESSION_IDLE_TTL_HOURS не число — беру 30", file=sys.stderr)
        ttl_hours = 30.0
    session_idle_ttl_sec = int(ttl_hours * 3600) if ttl_hours > 0 else 0

    model = os.environ.get("CLAUDE_MODEL", "").strip()
    raw_choices = os.environ.get("CLAUDE_MODEL_CHOICES", "").strip()
    model_choices = tuple(
        m for m in (x.strip() for x in raw_choices.split(",")) if m
    ) or DEFAULT_MODEL_CHOICES
    model_choices_dynamic = not raw_choices
    show_cost_footer = os.environ.get("SHOW_COST_FOOTER", "true").strip().lower() not in ("0", "false", "no", "off")
    try:
        context_warn_tokens = int(os.environ.get("CONTEXT_WARN_TOKENS", "200000") or "200000")
    except ValueError:
        print("[config] WARNING: CONTEXT_WARN_TOKENS не число — беру 200000", file=sys.stderr)
        context_warn_tokens = 200000

    # --- Codex (фоллбэк/альтернатива claude) ---
    # CODEX_BIN — абсолютный путь к нативному Linux-бинарю. Пусто = вся
    # codex-интеграция выключена (и фоллбэк, и ручной префикс).
    codex_bin = os.environ.get("CODEX_BIN", "").strip()
    if codex_bin and not os.path.isabs(codex_bin):
        print(
            f"[config] WARNING: CODEX_BIN={codex_bin!r} не абсолютный путь — "
            "под systemd PATH не наследуется, будет FileNotFoundError.",
            file=sys.stderr,
        )
    if codex_bin and not Path(codex_bin).exists():
        print(
            f"[config] WARNING: CODEX_BIN={codex_bin!r} не существует — "
            "codex-фоллбэк/gpt: упадут при первом вызове.",
            file=sys.stderr,
        )
    codex_fallback = os.environ.get("CODEX_FALLBACK", "true").strip().lower() not in ("0", "false", "no", "off")
    try:
        codex_timeout = int(os.environ.get("CODEX_TIMEOUT_SEC", "600") or "600")
    except ValueError:
        print("[config] WARNING: CODEX_TIMEOUT_SEC не число — беру 600", file=sys.stderr)
        codex_timeout = 600
    _codex_sandboxes = {"read-only", "workspace-write", "danger-full-access"}
    codex_sandbox = os.environ.get("CODEX_SANDBOX", "workspace-write").strip() or "workspace-write"
    if codex_sandbox not in _codex_sandboxes:
        raise RuntimeError(
            f"CODEX_SANDBOX={codex_sandbox!r} invalid, допустимо: {', '.join(sorted(_codex_sandboxes))}"
        )
    codex_fallback_sandbox = os.environ.get("CODEX_FALLBACK_SANDBOX", "read-only").strip() or "read-only"
    if codex_fallback_sandbox not in _codex_sandboxes:
        raise RuntimeError(
            f"CODEX_FALLBACK_SANDBOX={codex_fallback_sandbox!r} invalid, допустимо: {', '.join(sorted(_codex_sandboxes))}"
        )
    codex_network_access = os.environ.get("CODEX_NETWORK_ACCESS", "false").strip().lower() in ("1", "true", "yes", "on")
    if codex_network_access:
        for _var, _sb in (("CODEX_SANDBOX", codex_sandbox),
                          ("CODEX_FALLBACK_SANDBOX", codex_fallback_sandbox)):
            if _sb == "read-only":
                print(
                    f"[config] WARNING: CODEX_NETWORK_ACCESS=true, но {_var}=read-only — "
                    "в read-only sandbox сеть закрыта всегда, этот режим останется без сети.",
                    file=sys.stderr,
                )
    codex_prefix = os.environ.get("CODEX_PREFIX", "gpt:").strip() or "gpt:"

    return Config(
        telegram_bot_token=token,
        allowed_user_ids=allowed,
        allowed_chat_ids=allowed_chats,
        claude_bin=claude_bin,
        claude_cwd=claude_cwd,
        hook_settings_path=hook_settings,
        state_dir=state_dir,
        approve_whitelist_paths=approve_whitelist_paths,
        approve_auto_chat_keys=approve_auto_chat_keys,
        log_level=log_level,
        approve_http_host=approve_host,
        approve_http_port=approve_port,
        approve_timeout_sec=approve_timeout,
        approve_shared_secret=approve_secret,
        openai_api_key=openai_key,
        openai_tts_voice=openai_voice,
        openai_stt_model=openai_stt,
        openai_stt_prompt=openai_stt_prompt,
        openai_stt_keywords=openai_stt_keywords,
        openai_stt_languages=openai_stt_languages,
        openai_tts_model=openai_tts,
        openai_tts_instructions=openai_tts_instr,
        voice_reply_max_chars=voice_max,
        voice_max_duration_sec=voice_dur,
        telegram_text_limit=tg_text,
        telegram_caption_limit=tg_caption,
        large_reply_as_file_threshold=large_as_file,
        persona_text=persona_text,
        max_claude_runtime_sec=max_runtime,
        attachment_ttl_days=attach_ttl,
        photo_max_size_bytes=photo_max,
        document_max_size_bytes=document_max,
        telegram_api_id=tg_api_id,
        telegram_api_hash=tg_api_hash,
        download_min_free_bytes=min_free,
        big_download_timeout_sec=big_timeout,
        bash_auto_allow_re=bash_auto_allow_re,
        mcp_auto_allow_re=mcp_auto_allow_re,
        effort_level=effort_level,
        projects=projects,
        session_idle_ttl_sec=session_idle_ttl_sec,
        model=model,
        model_choices=model_choices,
        model_choices_dynamic=model_choices_dynamic,
        show_cost_footer=show_cost_footer,
        context_warn_tokens=context_warn_tokens,
        codex_bin=codex_bin,
        codex_fallback=codex_fallback,
        codex_timeout_sec=codex_timeout,
        codex_sandbox=codex_sandbox,
        codex_fallback_sandbox=codex_fallback_sandbox,
        codex_network_access=codex_network_access,
        codex_trigger_prefix=codex_prefix,
        run_silence_alert_sec=run_silence_alert,
        run_progress_note_sec=run_progress_note,
        autocompact=autocompact,
        autocompact_chats=autocompact_chats,
    )
