"""Регрессии запуска и скиллов (ревью 12.10.2026), без сети и реальных сессий."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_tg_bot import approval, claude_runner as runner, config, notify
from claude_tg_bot.skills.news_digest.core import Candidate, parse_answer
from claude_tg_bot.skills.yt_subs import __main__ as yt


def fake_config(**env):
    with (patch.object(config, 'load_dotenv'),
          patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '1:test',
                                  'TELEGRAM_ALLOWED_USER_IDS': '1',
                                  'CLAUDE_BIN': '/usr/bin/true', **env}, clear=True)):
        return config.load_config(skip_hook_validation=True)


def test_b16_writing_mcp_not_readonly():
    cfg = fake_config()
    for name in ('download_media', 'get_invite_link'):
        assert not approval._untrusted_tool_allowed(f'mcp__telegram__{name}', {}, cfg.mcp_auto_allow_re)
    assert approval._untrusted_tool_allowed('mcp__telegram__get_messages', {}, cfg.mcp_auto_allow_re)


async def run_fake(proc, **kw):
    store = MagicMock()
    store.get.return_value = None
    with (patch.object(runner, '_ensure_hook_still_valid'),
          patch.object(runner, 'ensure_oauth_fresh', AsyncMock()),
          patch.object(runner, 'agent_env', return_value={}),
          patch.object(runner.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)) as spawn):
        result = await runner.run_claude('claude', '1', 1, None, 'test', store,
                                        cwd=Path('/tmp'), hook_settings=Path('/tmp/fake'), **kw)
        return result, spawn.call_args.args


def fake_proc(event=None):
    proc = MagicMock(returncode=1, pid=123456789)
    proc.stdin.drain = AsyncMock()
    proc.wait = AsyncMock(return_value=1)
    proc.stdout = asyncio.StreamReader()
    if event:
        proc.stdout.feed_data((json.dumps(event) + '\n').encode())
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_eof()
    return proc


def test_b17_result_without_result():
    async def go():
        event = {'type': 'result', 'subtype': 'error_during_execution',
                 'is_error': True, 'errors': ['execution failed']}
        result, _ = await run_fake(fake_proc(event))
        assert result.is_error
        assert 'execution failed' in result.text
        assert '"type"' not in result.text
    asyncio.run(go())


def test_b19_allowlist_only_untrusted():
    async def go():
        with patch.object(runner, 'untrusted_settings_path', return_value=Path('/tmp/u.json')):
            _, args = await run_fake(fake_proc(), untrusted_sids=set())
        assert '--tools' not in args
        assert args[args.index('--allowedTools') + 1:args.index('--allowedTools') + 3] == ('Read', 'ToolSearch')
        assert args[args.index('--settings') + 1] == '/tmp/u.json'
        _, ordinary = await run_fake(fake_proc())
        assert '--allowedTools' not in ordinary and '--tools' not in ordinary
    asyncio.run(go())


@pytest.mark.parametrize('exc', [ValueError('line too long'), OSError('stream failed')])
def test_b5_3_stream_failure_kills_before_unregister(exc):
    async def go():
        proc = fake_proc()
        proc.returncode = None
        proc.stdout = SimpleNamespace(readline=AsyncMock(side_effect=exc),
                                      readuntil=AsyncMock(side_effect=exc))
        registry = {}
        def kill(p, sig):
            assert registry['1'] is p
        with patch.object(runner, 'kill_process_tree', side_effect=kill) as killed:
            with pytest.raises(type(exc)):
                await run_fake(proc, process_registry=registry)
        killed.assert_called_once()
        proc.wait.assert_awaited_once()
        assert not registry
    asyncio.run(go())


def test_oversized_stream_line_skipped(monkeypatch):
    # Строка длиннее лимита пропускается, ран доходит до result (ревью 11.10.2026).
    monkeypatch.setattr(runner, '_STREAM_LINE_LIMIT', 1024)
    event = {'type': 'result', 'subtype': 'success', 'is_error': False,
             'result': 'готово', 'session_id': 'sid-after-big-line'}
    script = ('import sys, json\n'
              'sys.stdout.write(\'{"type": "assistant", "x": "\' + "a" * 300000 + \'"}\\n\')\n'
              f'sys.stdout.write({json.dumps(json.dumps(event))} + "\\n")\n')
    real_exec = asyncio.create_subprocess_exec

    async def spawn(*args, **kw):
        return await real_exec(sys.executable, '-c', script, **kw)

    async def go():
        store = MagicMock()
        store.get.return_value = None
        with (patch.object(runner, '_ensure_hook_still_valid'),
              patch.object(runner, 'ensure_oauth_fresh', AsyncMock()),
              patch.object(runner, 'agent_env', return_value={}),
              patch.object(runner.asyncio, 'create_subprocess_exec', spawn)):
            return await runner.run_claude('claude', '1', 1, None, 'test', store,
                                           cwd=Path('/tmp'), hook_settings=Path('/tmp/fake'))
    result = asyncio.run(go())
    assert not result.is_error
    assert result.text == 'готово'
    assert result.session_id == 'sid-after-big-line'


@pytest.mark.parametrize('url', ['http://127.0.0.1/x', 'http://router.lan/x', 'http://192.168.1.1/x'])
def test_b18_input_url_rejected_before_fetch(url):
    with patch.object(yt, '_ytdlp') as fetch:
        with pytest.raises(SystemExit, match='публичная'):
            yt.main([url])
    fetch.assert_not_called()


@pytest.mark.parametrize('auto', [False, True])
def test_b18_track_url_rejected_before_download(auto):
    info = {'id': 'test', 'automatic_captions' if auto else 'subtitles': {
        'ru': [{'url': 'https://public.example/sub.vtt'}, {'url': 'http://127.0.0.1/private'}]}}
    with patch.object(yt, '_ytdlp', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(info))) as fetch:
        with pytest.raises(SystemExit, match='непубличный'):
            yt.main(['https://public.example/video'])
    assert fetch.call_count == 1


@pytest.mark.parametrize('item', ['string', {'title': 42, 'refs': ['c1']},
                                   {'title': 'test', 'refs': ['c1', {}], 'detail': [], 'kind': []}])
def test_b22_malformed_model_fields(item):
    candidates = [Candidate('id', 'source', 'focus', 'candidate', 'https://public.example')]
    items = parse_answer(json.dumps({'items': [item]}), candidates)
    if isinstance(item, dict) and item.get('title') == 'test':
        assert len(items) == 1
        assert items[0].detail == ''
    else:
        assert items == []


@pytest.mark.parametrize('name,value', [('TELEGRAM_TEXT_LIMIT', '0'), ('TELEGRAM_TEXT_LIMIT', '-1'),
                                       ('TELEGRAM_TEXT_LIMIT', '4097'), ('TELEGRAM_CAPTION_LIMIT', '0'),
                                       ('TELEGRAM_CAPTION_LIMIT', '1025'), ('VOICE_REPLY_MAX_CHARS', '0')])
def test_b5_6_invalid_limits_rejected(name, value):
    with pytest.raises(RuntimeError, match=name):
        fake_config(**{name: value})


@pytest.mark.parametrize('limit', [0, -1])
def test_b5_6_notify_invalid_limit_no_send(limit):
    cfg = SimpleNamespace(primary_chat_id=1, telegram_bot_token='1:test', telegram_text_limit=limit)
    with patch.object(notify, 'load_config', return_value=cfg), patch.object(notify.httpx, 'post') as post:
        assert notify.send_message(['test'])[0] == 1
    post.assert_not_called()


