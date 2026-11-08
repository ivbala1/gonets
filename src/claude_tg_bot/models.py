"""Живой список моделей Claude: подписи к кнопкам /model.

Кнопки /model выбирают алиас семейства (fable, opus, sonnet, haiku), а
свежую модель по алиасу подставляет сам CLI. Отсюда берётся только подпись:
какая модель семейства сейчас новейшая по API (`GET /v1/models` тем же
OAuth-логином Claude Code, которым ходит CLI). Ответ кэшируется в
`state/models.json` на сутки; при любой ошибке (сеть, протухший токен, нет
файла, битый кэш) остаётся прошлый кэш, а без него кнопки идут без подписи.
"""
from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import jsonstore
from .claude_runner import _CREDENTIALS_PATH, oauth_credentials

log = logging.getLogger(__name__)

MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
CACHE_NAME = "models.json"
CACHE_TTL_SEC = 24 * 3600
FAMILY_ORDER = ("fable", "opus", "sonnet", "haiku")
_FAMILY_RE = re.compile(r"^claude-([a-z]+)-")


def _access_token(path: Path = _CREDENTIALS_PATH) -> str | None:
    """OAuth access-token Claude Code; None без файла или при логине по ключу."""
    oauth = oauth_credentials(path)
    token = oauth.get("accessToken") if oauth else None
    return token if isinstance(token, str) and token else None


def fetch_models(timeout: float = 10.0) -> list[dict]:
    """Список моделей у API: [{id, display_name, created_at}], новые первыми.
    Ошибка сети, ответа или отсутствие токена это OSError/ValueError."""
    token = _access_token()
    if not token:
        raise OSError("нет OAuth-токена Claude Code в .credentials.json")
    request = urllib.request.Request(MODELS_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OSError(f"HTTP {exc.code} от /v1/models") from exc
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("ответ /v1/models без списка data")
    models = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append({"id": item["id"], "display_name": item.get("display_name") or item["id"],
                           "created_at": item.get("created_at") or ""})
    return models


def family(model_id: str) -> str | None:
    match = _FAMILY_RE.match(model_id)
    return match.group(1) if match else None


def newest_by_family(models: list[dict]) -> dict[str, str]:
    """Семейство -> id самой свежей его модели, в порядке FAMILY_ORDER.
    created_at это ISO-дата, поэтому сравнивается строкой."""
    best: dict[str, dict] = {}
    for model in models:
        model_id = model.get("id") if isinstance(model, dict) else None
        kind = family(model_id) if isinstance(model_id, str) else None
        if kind not in FAMILY_ORDER:
            continue
        current = best.get(kind)
        if current is None or (model.get("created_at") or "") > (current.get("created_at") or ""):
            best[kind] = model
    return {kind: best[kind]["id"] for kind in FAMILY_ORDER if kind in best}


def cached_models(state_dir: Path, *, ttl_sec: float = CACHE_TTL_SEC,
                  fetch=fetch_models, now=time.time) -> list[dict]:
    """Модели из кэша, пока он свежий; иначе запрос к API с обновлением
    кэша. Ошибка запроса оставляет прошлый кэш (пусть и старый) либо []."""
    cache_path = state_dir / CACHE_NAME
    cached = jsonstore.read_dict(cache_path, keep_corrupt=False)  # кэш: копия битого не нужна
    stale_models = cached.get("models") if isinstance(cached.get("models"), list) else []
    fetched_at = cached.get("fetched_at") if isinstance(cached.get("fetched_at"), (int, float)) else 0
    if stale_models and now() - fetched_at < ttl_sec:
        return stale_models
    try:
        models = fetch()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("список моделей не обновился (%s); беру %s", exc,
                    "прошлый кэш" if stale_models else "пустой список")
        return stale_models
    try:
        jsonstore.write_json(cache_path, {"fetched_at": now(), "models": models}, indent=1)
    except OSError as exc:
        log.warning("кэш моделей не записался: %s", exc)
    return models


def live_newest_by_family(state_dir: Path) -> dict[str, str]:
    """Подписи к кнопкам /model: семейство -> новейшая модель по API.
    Пустой словарь, когда списка нет ни у API, ни в кэше."""
    return newest_by_family(cached_models(state_dir))
