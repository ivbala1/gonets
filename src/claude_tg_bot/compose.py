"""Накопитель /compose … /go (перенесён из bot.py, шаг 1 разбиения)."""
from __future__ import annotations

from dataclasses import dataclass, field

from telegram.ext import ContextTypes


@dataclass
class ComposeBuffer:
    """Накопитель кусков для /compose … /go.

    generation отличает буфер от следующего такого же в этой теме: без него
    обработчик, который проснулся после `/compose cancel` + нового `/compose`,
    писал бы кусок по тому же индексу уже в ЧУЖОЙ буфер.
    sealed ставит первый /go, дальше буфер только дозаполняется — второй /go
    не отправит то же самое повторно.
    cancelled ставит /compose cancel: /go, ждущий куски, ничего не отправляет.
    Голосового флага у буфера нет (с 08.11.2026): ответ после /go озвучивает
    метка <<VOICE>>, если об этом просят свои слова, а не пересланные.
    """
    generation: int
    items: list[str | None] = field(default_factory=list)
    sealed: bool = False
    cancelled: bool = False
    # Идущие большие загрузки (MTProto) в слотах буфера: /go ждёт их дольше 30 с.
    slow: int = 0

    @property
    def ready(self) -> bool:
        return all(x is not None for x in self.items)

    @property
    def filled(self) -> int:
        return sum(1 for x in self.items if x is not None)


# Резервация места: буфер идентифицируется поколением, слот — индексом.
Reservation = tuple[int, int]


def _compose_buffers(ctx: ContextTypes.DEFAULT_TYPE) -> dict[str, ComposeBuffer]:
    return ctx.application.bot_data.setdefault("compose_buffers", {})


def _compose_reserve(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str) -> Reservation | None:
    """Занять место в буфере ДО долгой обработки (STT, скачивание вложения).

    Без резерва /go, нажатый пока идёт распознавание голоса, отправлял бы
    неполный промпт, а опоздавший кусок уезжал бы отдельным раном.
    None = буфер не активен (или уже запечатан) — обработчик работает обычным путём.
    """
    buf = _compose_buffers(ctx).get(chat_key)
    if buf is None or buf.sealed:
        return None
    buf.items.append(None)  # placeholder, заполнится в _compose_fill
    return (buf.generation, len(buf.items) - 1)


def _compose_current(
    ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, res: Reservation
) -> ComposeBuffer | None:
    """Буфер, если он тот же самый (не отменён и не заменён новым)."""
    buf = _compose_buffers(ctx).get(chat_key)
    if buf is None or buf.generation != res[0] or res[1] >= len(buf.items):
        return None
    return buf


def _compose_fill(
    ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, res: Reservation, piece: str,
) -> int | None:
    """Заполнить зарезервированный слот. None — если буфер уехал по /go или был
    отменён (тогда обработчик шлёт кусок обычным путём, не теряя его; кусок
    отменённого буфера выбрасывает, см. _compose_cancelled)."""
    buf = _compose_current(ctx, chat_key, res)
    if buf is None:
        return None
    buf.items[res[1]] = piece
    return buf.filled


def _compose_cancelled(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, res: Reservation | None) -> bool:
    """Слот из буфера, отменённого /compose cancel. Опоздавший кусок такого
    буфера выбрасывается, как в отменённой пачке склейки: иначе голосовое,
    распознанное после отмены, уходило бы отдельным раном. Кусок буфера,
    уехавшего по /go без него (таймаут), по-прежнему шлётся (ревью 12.10.2026)."""
    if res is None:
        return False
    cancelled: dict[str, set[int]] = ctx.application.bot_data.get("compose_cancelled") or {}
    return res[0] in cancelled.get(chat_key, ())


def _compose_release(ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, res: Reservation | None) -> None:
    """Освободить слот, если кусок не доехал (сбой STT/скачивания).

    Без этого /go ждал бы 30 секунд заведомо пустое место.
    """
    if res is None:
        return
    buf = _compose_current(ctx, chat_key, res)
    if buf is not None and buf.items[res[1]] is None:
        buf.items[res[1]] = ""  # пустое = «места нет», при сборке отфильтруется


def _compose_add(
    ctx: ContextTypes.DEFAULT_TYPE, chat_key: str, piece: str,
) -> int | None:
    """Резерв+заполнение одним шагом: для текста, где долгой обработки нет."""
    res = _b._compose_reserve(ctx, chat_key)
    if res is None:
        return None
    return _compose_fill(ctx, chat_key, res, piece)


# В конце: bot импортирует этот модуль при своей загрузке. Имена bot и
# патчимые тестами на bot зовутся как _b.X, чтобы patch.object(bot, ...) работал.
from . import bot as _b  # noqa: E402
