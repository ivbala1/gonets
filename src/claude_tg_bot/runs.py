"""Номер запуска процесса claude и разрешение на план, выданное этому запуску.

Номер сессии (sid) живёт через много запусков (`--resume`), поэтому права
«до конца хода» привязаны к номеру запуска: claude_runner заводит его при
старте процесса и снимает в finally, вместе с разрешением. Нажатие на
карточку плана выдаёт разрешение только тому запуску, который её запросил и
ещё идёт (проект шага 2, ревью Codex 29.10.2026).

Состояние в памяти процесса бота: рестарт бота обрывает и сами запуски.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

# Аварийный потолок разрешения на план: граница основная конец запуска.
PLAN_MAX_SEC = 2 * 60 * 60

# sid -> идущие запуски. Два запуска одной сессии разом (обычно его не дают
# замки темы) номера не получают: чьё нажатие, не различить (ревью Codex 29.10).
_active: dict[str, list[str]] = {}
_plans: dict[str, "PlanGrant"] = {}


@dataclass(frozen=True)
class PlanGrant:
    plan_aid: str
    until: float
    # Текст одобренного плана: названные в нём хосты считаются шагом работы.
    text: str = ""


def start(sid: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    _active.setdefault(sid, []).append(run_id)
    return run_id


def _drop(sid: str, run_id: str) -> None:
    runs = _active.get(sid)
    if runs and run_id in runs:
        runs.remove(run_id)
        if not runs:
            _active.pop(sid, None)


def end(sid: str, run_id: str) -> None:
    _drop(sid, run_id)
    _plans.pop(run_id, None)


def rebind(old: str, new: str, run_id: str) -> None:
    """Ответвление сессии: запуск тот же, номер сессии настоящий."""
    if run_id in _active.get(old, ()):
        _drop(old, run_id)
        _active.setdefault(new, []).append(run_id)


def current(sid: str | None) -> str:
    runs = _active.get(sid) if sid else None
    return runs[0] if runs and len(runs) == 1 else ""


def grant_plan(sid: str | None, run_id: str, plan_aid: str, text: str = "") -> bool:
    """False: запуск, запросивший план, уже кончился или сменился."""
    if not run_id or current(sid) != run_id:
        return False
    _plans[run_id] = PlanGrant(plan_aid, time.time() + PLAN_MAX_SEC, text)
    return True


def revoke_plan(run_id: str) -> None:
    """«❌ Не надо» на новом плане снимает и прежний: Иван отказал в работе
    этого запуска, а не только в расширении (ревью 29.10.2026)."""
    _plans.pop(run_id, None)


def plan_for(sid: str | None) -> PlanGrant | None:
    run_id = current(sid)
    grant = _plans.get(run_id) if run_id else None
    if grant is None:
        return None
    if time.time() >= grant.until:
        _plans.pop(run_id, None)
        return None
    return grant
