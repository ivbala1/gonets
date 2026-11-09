"""Правила хранения каталога state/ бота и отчёт суточной уборки (этап 1
уборки временного, решение Ивана 09.11.2026).

У каждого вида данных есть правило: срок, после которого уборка бота
(`bot._attachment_cleanup_loop`) его удаляет, или пометка, почему оно
хранится («постоянно», «справочник»). Имя в state/, которого нет в RULES или
с пометкой STAGE2 («этап 2», срок не назначен), попадает в отчёт как «без правила», и его
показывает doctor Свода (`check_state_cleanup`).

Отчёт `state/cleanup/last.json` в общем виде с очистителем ~/tmp Свода:
finished_at, deleted_files, freed_bytes, errors, error_sample, плюс overdue
(файлов старше срока осталось после прохода) и unknown (имена без правила).
"""
from __future__ import annotations

import fnmatch
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore, paths

REPORT = Path("cleanup") / "last.json"
TWO_MIND_TTL_DAYS = 30
# Корни сервера telegram-mcp вне state/ (продолжение этапа 1, 09.11.2026):
# владелец бот, срок от последнего изменения (age_time), не зависит от
# ATTACHMENT_TTL_DAYS. Папка отправки: копии для send_file и вложения mailsend
# (paths.TELEGRAM_SEND_DIR); кэш: скачанное download_media. Системный tmpfiles для кэша
# пропал незаметно до 10.10.2026, поэтому уборка здесь, с отчётом для doctor.
TELEGRAM_FILES_TTL_DAYS = 30
# Из кэша только скачанное (download_media пишет в <корень>/downloads): в самом
# корне агенты держат свои файлы (аудио зала, документы), им срок не назначен.
TELEGRAM_MCP_DOWNLOADS = Path.home() / ".cache" / "telegram-mcp" / "downloads"


# Корни, куда агенты переносят и копируют файлы руками: там срок от позднего из
# mtime и ctime, иначе перенесённый (mv) или скопированный с датой (cp -p)
# старый файл ушёл бы первым же проходом. В state/ и у сервера Bot API файлы
# пишет сам бот: срок от mtime.
COPY_ROOTS = frozenset({paths.TELEGRAM_SEND_DIR, TELEGRAM_MCP_DOWNLOADS})
# То же для деревьев state/ по имени: в outbox агенты кладут файлы руками
# для <<SEND_FILE:...>> (решение Ивана 09.11.2026: 30 дней, как telegram-send).
COPY_STATE_NAMES = frozenset({"outbox"})


def age_time(st, root: Path) -> float:
    """Время, от которого считается срок файла дерева root."""
    copied = root in COPY_ROOTS or root.name in COPY_STATE_NAMES
    return max(st.st_mtime, st.st_ctime) if copied else st.st_mtime


def trees(state_dir: Path, attachment_ttl_days: int) -> list[tuple[Path, int, str]]:
    """Деревья суточной уборки: (корень, срок в днях, имя для журнала)."""
    return [
        (state_dir / "attachments", attachment_ttl_days, "attachments"),
        # Черновики записей памяти: тело уже в git памяти, файл нужен минуты.
        (state_dir / "memory-drafts", attachment_ttl_days, "memory-drafts"),
        # Запросы и ответы /2mind: нужны рану, который спросил; позже их не читают.
        (state_dir / "2mind", TWO_MIND_TTL_DAYS, "2mind"),
        # Этап 2 (09.11.2026): сроки владельцев, которые сами чистят только при
        # следующей записи; здесь тем же сроком и без активности.
        (state_dir / "voice-texts", _full_text_days(), "voice-texts"),
        (state_dir / "mailsend", MAILSEND_TTL_DAYS, "mailsend"),
        (state_dir / "outbox", TELEGRAM_FILES_TTL_DAYS, "outbox"),
        (paths.TELEGRAM_SEND_DIR, TELEGRAM_FILES_TTL_DAYS, "telegram-send"),
        (TELEGRAM_MCP_DOWNLOADS, TELEGRAM_FILES_TTL_DAYS, "telegram-mcp-downloads"),
    ]


# Билет одобрения письма живёт mailsend.TICKET_TTL_SEC (10 минут); сутки с запасом.
MAILSEND_TTL_DAYS = 1
# Итоговые записи очереди «в приложение» (сделано, снято): дней от закрытия.
APP_QUEUE_CLOSED_DAYS = 30
# Итоговые отклики (отправлено, пропущено, не ушло, по otklik.DONE_STATUSES;
# у failed есть кнопка ↩️, после срока она ответит «устарела»): дней от
# последнего изменения записи (решение Ивана 09.11.2026); остальные не удаляются.
OTKLIK_DONE_DAYS = 180
# Пометка «срок не назначен»: в отчёте идёт как имя без правила (doctor красный).
STAGE2 = "этап 2"


def _full_text_days() -> int:
    from .replies import _FULL_TEXT_TTL_SEC
    return _FULL_TEXT_TTL_SEC // 86400


def _handoff_prune(handoff, state_dir: Path, now: float, dry_run: bool) -> int:
    base = handoff.handoff_dir(state_dir)
    if base.is_symlink() or not base.is_dir():
        return 0
    return handoff.prune(base, now, dry_run=dry_run)


def owner_passes(state_dir: Path, now: float, *, dry_run: bool = False) -> tuple[dict[str, int], list[str]]:
    """Уборка словарей и записей их владельцами, которые сами чистят только при
    рестарте или следующей записи (этап 2): ({проход: удалено записей}, ошибки).
    Записи, а не файлы: в отчёте они идут в deleted_files без freed_bytes.
    Сухой прогон только считает; подрезку sent_texts не показывает."""
    from . import events, handoff, otklik, reactions
    try:
        from . import app_launch
    except ImportError:  # открытая копия: очереди «в приложение» в ней нет
        app_launch = None
    counts, errors = {}, []
    passes = [
        ("events", lambda: events.prune_done(events.event_dir(state_dir), now, dry_run=dry_run)),
        # Экраны передачи по created_ts записи; замок `.lock` каталога не трогается.
        ("handoff", lambda: _handoff_prune(handoff, state_dir, now, dry_run)),
        ("otkliki", lambda: otklik.prune(otklik.otklik_dir(state_dir), now,
                                         OTKLIK_DONE_DAYS * 86400, dry_run=dry_run)),
    ]
    if app_launch is not None:
        passes.append(("app_queue", lambda: app_launch.prune(
            state_dir, now, APP_QUEUE_CLOSED_DAYS * 86400, dry_run=dry_run)))
    if not dry_run:
        # Подрезка не удаляет файлов: в счёт удалённых не идёт.
        passes.append(("sent_texts", lambda: reactions.trim(state_dir) and 0))
    for name, fn in passes:
        # Любой сбой одного прохода идёт в отчёт, а не обрывает весь проход
        # до write_report (ревью 09.11.2026: TypeError из битой записи).
        try:
            counts[name] = int(fn())
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
    return counts, errors


def due_files(roots: list[tuple[Path, int, str]], now: float) -> list[tuple[str, Path, float]]:
    """Что удалит проход (для сухого прогона): (имя дерева, файл, время срока).
    Тот же отбор, что у bot._cleanup_tree: по lstat, без входа в ссылки."""
    import os
    out = []
    for root, ttl, name in roots:
        if ttl <= 0 or root.is_symlink() or not root.is_dir():
            continue
        cutoff = now - ttl * 86400
        for top, dirs, names in os.walk(root, followlinks=False):
            for n in names + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
                f = Path(top, n)
                try:
                    m = age_time(f.lstat(), root)
                except OSError:
                    continue
                if m < cutoff:
                    out.append((name, f, m))
    return out

# Имя или шаблон верхнего уровня state/ -> правило. Порядок не важен.
RULES: dict[str, str] = {
    "attachments": "уборка бота: ATTACHMENT_TTL_DAYS от записи",
    "memory-drafts": "уборка бота: ATTACHMENT_TTL_DAYS от записи",
    "2mind": f"уборка бота: {TWO_MIND_TTL_DAYS} дней от записи (запросы и ответы /2mind, позже их никто не читает)",
    "audit.jsonl": "прежний общий журнал действий: разносится по месяцам первой уборкой",
    "audit-*.jsonl": "журнал действий по месяцам: audit.KEEP_MONTHS месяцев",
    "cleanup": "отчёт этой уборки",
    "*.lock": "замок: пустой файл, постоянно",
    "audit.jsonl.split.lock": "замок разноса журнала: пустой файл",
    "*.bak-*": "копия до ручной правки: удалять по «ок» Ивана",
    "telethon-bot.session*": "постоянно: вход Telethon (и журнал SQLite во время записи)",
    ".writetest-*": "проба записи при старте (__main__), живёт секунды",
    "geodata": "справочник: постоянно",
    # Этап 2 (09.11.2026): владелец бот, срок по коду владельца.
    "events": "уборка бота: конченные события events.KEEP_DONE_SEC (8 суток) после done_at; живые постоянно",
    "voice-texts": "уборка бота: полный текст ответа replies._FULL_TEXT_TTL_SEC (14 дней) от записи",
    "handoff": "уборка бота: экраны передачи handoff.prune, handoff.KEEP_DAYS (7 дней) от created_ts; замок постоянно",
    "mailsend": f"уборка бота: билеты одобрения письма {MAILSEND_TTL_DAYS} день от записи (живут 10 минут)",
    "news_digest": ("чистит владелец: выпуски 30 дней, seen 60, задачи gone 40 (skills/news_digest);"
                    " отзывы feedback.jsonl и замки постоянно, растут медленно"),
    "deals": "чистит владелец: закрытые дела deals.KEEP_CLOSED_SEC (8 суток) при старте бота",
    "sent_texts.jsonl": ("уборка бота: хвост 256 КБ для reactions.lookup (текст под реакцией Ивана),"
                         " подрезка в суточном проходе; в защите от повторов не участвует"),
    "session_scopes.json": "чистит владелец: 90 дней от последнего рана сессии (не короче жизни расшифровок, изоляция /resume)",
    "untrusted_sessions.json": "чистит владелец: 90 дней от последнего рана помеченной сессии, как session_scopes (метка не короче окна /resume)",
    "codex_checks.json": "чистит владелец: последние 30 кнопок «Проверь Кодексом» (tg_quick.CHECK_KEEP)",
    "session_costs.json": "чистит владелец: последние 300 сессий (claude_runner.COST_TOTALS_KEEP)",
    "cost_by_day.json": "чистит владелец: 14 дней",
    "daily_pins.json": "чистит владелец: 40 дней (notify.DAILY_KEEP_SEC)",
    "app_queue.json": f"уборка бота: сделанные и снятые записи {APP_QUEUE_CLOSED_DAYS} дней после закрытия; открытые постоянно",
    "at_jobs.json": "постоянно: очередь /at, записи снимаются при запуске и отмене",
    "sessions.json": "постоянно: сессия на тему, снимается /reset и закрытием задачи",
    "scope_fingerprints.json": "постоянно: запись на тему, снимается закрытием задачи",
    "topic_names.json": "постоянно: имя на тему, снимается закрытием задачи",
    "model_session_overrides.json": "постоянно: снимается сменой сессии",
    "foreign_sessions.json": "постоянно: запись на чат, перезаписывается каждым раном",
    "deal_sessions.json": "постоянно: запись на чат, перезаписывается каждым раном",
    "reaction_notes.json": "постоянно: опустошается следующим ходом лички",
    # Ждут решения Ивана (вопрос 09.11.2026): невоспроизводимое или без копии.
    # Решение Ивана 09.11.2026.
    "otkliki": (f"уборка бота: итоговые по otklik.DONE_STATUSES (sent, skipped, failed, включая failed"
                f" с кнопкой ↩️) {OTKLIK_DONE_DAYS} дней от updated; остальные постоянно; нерешённые пачки"
                " batches 7 дней (otklik.BATCH_KEEP_SEC); замок .lock постоянно"),
    "outbox": "уборка бота: файлы для SEND_FILE 30 дней от позднего из mtime и ctime, как telegram-send",
    # Остатки дневника бота (код снят 07.10.2026, канон в Obsidian): удалить,
    # «ок» Ивана 09.11.2026; снимок ~/tmp/2026-11-09-uborka-snimok. После удаления правила снять.
    "diary": "остаток дневника бота: удалить («ок» Ивана 09.11.2026)",
    "diary_audio": "остаток дневника бота: удалить («ок» Ивана 09.11.2026)",
    "diary_evening": "остаток дневника бота: удалить («ок» Ивана 09.11.2026)",
    "diary_snooze.json": "остаток дневника бота: удалить («ок» Ивана 09.11.2026)",
    "claude_limits.json": "постоянно: одна запись",
    "login_expiry.json": "постоянно: одна запись", "model_overrides.json": "постоянно: настройки",
    "model_pending_overrides.json": "постоянно: настройки",
    "models.json": "постоянно: настройки",
    "scopes.json": "постоянно: настройки",
    "inflight.json": "постоянно: идущие раны", "effort_overrides.json": "постоянно: настройки",
    "restart_request.json": "заявка на перезапуск: живёт до перезапуска",
}


def rule_for(name: str) -> str | None:
    if name in RULES:
        return RULES[name]
    return next((rule for pat, rule in RULES.items() if fnmatch.fnmatchcase(name, pat)), None)


def unknown(state_dir: Path) -> list[str]:
    """Имена верхнего уровня state/ без правила (скрытые временные `.имя.tmp`
    записи jsonstore не в счёт)."""
    try:
        names = sorted(p.name for p in state_dir.iterdir())
    except OSError:
        return []
    return [n for n in names if rule_for(n) in (None, STAGE2) and not (n.startswith(".") and n.endswith(".tmp"))]


def write_report(state_dir: Path, *, deleted: int, freed: int, errors: int, sample: list[str],
                 overdue: int) -> None:
    jsonstore.write_json(state_dir / REPORT, {
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(state_dir), "deleted_files": deleted, "freed_bytes": freed,
        "errors": errors, "error_sample": sample[:3], "overdue": overdue,
        "unknown": unknown(state_dir),
        "python": ".".join(map(str, sys.version_info[:3])),
    }, indent=1)


def main(argv: list[str] | None = None) -> int:
    """python -m claude_tg_bot.retention --dry-run: что удалит следующий проход."""
    import argparse
    import time

    from dotenv import load_dotenv

    from . import config
    # Те же сроки и каталог, что у бота: его настройки из .env (значения не печатаются).
    load_dotenv(paths.ENV_FILE)
    ap = argparse.ArgumentParser(prog="claude_tg_bot.retention")
    ap.add_argument("--dry-run", action="store_true", required=True,
                    help="только показать; удаляет суточный цикл бота")
    ap.parse_args(argv)
    ttl = config._int_env("ATTACHMENT_TTL_DAYS", 30)
    roots = trees(paths.state_dir(), ttl)
    now = time.time()
    due = due_files(roots, now)
    for root, days, name in roots:
        print(f"{name}: {root}, срок {days} дн.")
    print(f"удалит {len(due)}:")
    for name, f, m in due:
        print(f"  {name}  {datetime.fromtimestamp(m, config.local_zone()):%d.%m.%Y}  {f}")
    counts, errs = owner_passes(paths.state_dir(), now, dry_run=True)
    print("проходы владельцев удалят записей: "
          + ", ".join(f"{k} {v}" for k, v in counts.items())
          + (f"; ошибки: {', '.join(errs)}" if errs else ""))
    unk = unknown(paths.state_dir())
    if unk:
        print("без правила: " + ", ".join(unk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
