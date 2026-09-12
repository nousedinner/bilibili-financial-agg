"""Isolated acceptance checks. No production network or database access."""
import sys
from pathlib import Path

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

for k, v in {'DB_USER': 'review', 'DB_PASSWORD': 'review', 'DB_HOST': '127.0.0.1', 'XIAOMI_API_KEY': 'TEST_ONLY', 'SESSDATA': ''}.items():
    os.environ[k] = v

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src import config
config._CONFIG = {'bloggers': [], 'data': {'first_run_cap': 2, 'backfill_since': '2026-01-01'}, 'limits': {'video_interval_seconds': 0}}
from src import main, fetcher, analyzer, bilibili, transcript
from src.models import Base, Blogger, Video, Summary, Dynamic, DailyDigest, Transcript, PendingDynamic, DirtyDigest, FetchJob, DanmakuAnalysis, CommentAnalysis, FetchWatermark


class Acceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch.object(main, 'async_session', self.sessions), patch.object(fetcher, 'async_session', self.sessions)]
        for p in self.patches:
            p.start()
        main.app.dependency_overrides[main.require_api_key] = lambda: None
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://review.invalid')

    async def asyncTearDown(self):
        await self.http.aclose()
        main.app.dependency_overrides.clear()
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def seed_videos(self, tied=False):
        t = datetime(2026, 9, 9, 12)
        async with self.sessions() as s:
            s.add(Blogger(mid=1, name='Review', tags=[]))
            for i in range(3):
                s.add(Video(bvid=f'BVreview{i}', mid=1, title='Review', publish_time=t if tied else t - timedelta(minutes=i), fetch_status='ok', analysis_status='completed'))
            await s.commit()

    def client(self):
        return SimpleNamespace(get_video_info=AsyncMock(return_value={'cid': 1, 'aid': 2}), get_comments=AsyncMock(return_value={'replies': [], 'total': 0}), get_danmaku=AsyncMock(return_value={'items': [], 'total': 0}))

    async def test_feed_full_video_page_has_more(self):
        await self.seed_videos()
        data = (await self.http.get('/api/feed', params={'limit': 2})).json()['data']
        self.assertTrue(data['has_more'])

    async def test_feed_cursor_matches_android_serialized_name(self):
        await self.seed_videos()
        data = (await self.http.get('/api/feed', params={'limit': 2})).json()['data']
        self.assertIsNotNone(data.get('next_cursor'), 'Android reads next_cursor; response keys=' + str(list(data)))

    async def test_feed_tied_timestamp_keeps_third_item(self):
        await self.seed_videos(tied=True)
        first = (await self.http.get('/api/feed', params={'limit': 2})).json()['data']
        second = (await self.http.get('/api/feed', params={'limit': 2, 'before': first['cursor']['before'], 'before_id': first['next_cursor']['before_id']})).json()['data']
        self.assertEqual(len(second['items']), 1)

    async def test_feed_excludes_disabled_dynamic(self):
        async with self.sessions() as s:
            s.add(Blogger(mid=1, name='Disabled', tags=[], enabled=False))
            s.add(Dynamic(dyn_id='test', mid=1, publish_time=datetime.now(), content='test'))
            await s.commit()
        data = (await self.http.get('/api/feed')).json()['data']
        self.assertEqual(data['items'], [])

    async def test_empty_cid_is_persisted_failed(self):
        c = self.client()
        c.get_video_info.return_value = {}
        with self.assertRaises(ValueError):
            await fetcher._process_video(c, 1, {'bvid': 'BVcid', 'created': 1785513600, 'length': 1})
        async with self.sessions() as s:
            v = await s.get(Video, 'BVcid')
            self.assertEqual(v.fetch_status, 'failed')
            self.assertEqual(v.retry_count, 1)

    async def test_exception_before_save_is_persisted_failed(self):
        c = self.client()
        c.get_video_info.side_effect = bilibili.BiliAPIError(-404, 'mock missing video')
        with self.assertRaises(bilibili.BiliAPIError):
            await fetcher._process_video(c, 1, {'bvid': 'BVexception', 'created': 1785513600, 'length': 1})
        async with self.sessions() as s:
            v = await s.get(Video, 'BVexception')
            self.assertEqual(v.fetch_status, 'failed')

    async def test_asr_exception_is_persisted_failed(self):
        with patch.object(fetcher, 'fetch_transcript', AsyncMock(side_effect=TimeoutError('mock audio timeout'))):
            with self.assertRaises(TimeoutError):
                await fetcher._process_video(self.client(), 1, {'bvid': 'BVasr', 'created': 1785513600, 'length': 1})
        async with self.sessions() as s:
            self.assertEqual((await s.get(Video, 'BVasr')).fetch_status, 'failed')

    async def test_video_llm_failure_preserves_summary_and_marks_failed(self):
        async with self.sessions() as s:
            s.add(Summary(bvid='BVllm', summary='previous good result'))
            await s.commit()
        with patch.object(fetcher, 'fetch_transcript', AsyncMock(return_value={'text': 'valid transcript', 'source': 'asr', 'segments': 1})), patch.object(fetcher, 'analyze_video', AsyncMock(return_value=analyzer._empty_analysis(analysis_failed=True))):
            with self.assertRaises(ValueError):
                await fetcher._process_video(self.client(), 1, {'bvid': 'BVllm', 'created': 1785513600, 'length': 1})
        async with self.sessions() as s:
            self.assertEqual((await s.get(Video, 'BVllm')).fetch_status, 'failed')
            self.assertEqual((await s.get(Summary, 'BVllm')).summary, 'previous good result')

    async def test_failed_dynamic_is_not_saved_as_completed(self):
        payload = {'id_str': 'dynfail', 'modules': {'module_dynamic': {'desc': {'text': 'mock dynamic'}}, 'module_author': {'pub_ts': 1785513600}}}
        with patch.object(fetcher, 'analyze_dynamic', AsyncMock(return_value={'summary': '', 'sentiment': 'neutral', 'tags': [], 'analysis_failed': True})):
            with self.assertRaises(ValueError):
                await fetcher._process_dynamic(None, 1, payload)
        async with self.sessions() as s:
            self.assertIsNone(await s.get(Dynamic, 'dynfail'))
            self.assertEqual((await s.get(PendingDynamic, 'dynfail')).retry_count, 1)

    async def test_failed_digest_does_not_replace_existing_content(self):
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        pub = datetime.combine(today, datetime.min.time()).replace(hour=12)
        async with self.sessions() as s:
            s.add(Video(bvid='BVdaily', mid=1, title='daily', publish_time=pub, fetch_status='ok'))
            s.add(Summary(bvid='BVdaily', summary='good video', key_points=[], sentiment='neutral', sentiment_score=0))
            s.add(DailyDigest(digest_date=today, content={'summary': 'previous good digest'}))
            await s.commit()
        with patch.object(fetcher, 'generate_daily_digest', AsyncMock(return_value=analyzer._empty_digest())):
            with self.assertRaises(ValueError):
                await fetcher._generate_digest([{'mid': 1, 'name': 'Review'}], {}, target_date=today)
        async with self.sessions() as s:
            self.assertEqual((await s.get(DailyDigest, today)).content['summary'], 'previous good digest')

    async def test_database_only_blogger_can_backfill(self):
        async with self.sessions() as s:
            s.add(Blogger(mid=42, name='Added through API', tags=[]))
            await s.commit()
        with patch.object(bilibili.BiliClient, 'get_video_list', AsyncMock(return_value={'list': {'vlist': []}})) as listing:
            result = await fetcher.run_backfill(42, '2026-09-01')
            listing.assert_awaited_once()
        self.assertNotIn('error', result)

    async def test_manual_and_scheduled_fetch_are_mutually_exclusive(self):
        entered = 0
        both_started = asyncio.Event()
        release = asyncio.Event()
        async def fake_fetch():
            nonlocal entered
            entered += 1
            if entered >= 2:
                both_started.set()
            await release.wait()
            return {'total_new': 0}
        with patch.object(main, 'run_fetch_only', fake_fetch):
            tasks = [asyncio.create_task(main._run_fetch_task()), asyncio.create_task(main._scheduled_fetch())]
            try:
                try:
                    await asyncio.wait_for(both_started.wait(), timeout=0.1)
                except TimeoutError:
                    pass
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(entered, 1)

    async def test_health_db_failure_returns_unhealthy_http_status(self):
        class BadDB:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def execute(self, *args): raise RuntimeError('mock DB unavailable')
        with patch.object(main, 'async_session', BadDB):
            response = await self.http.get('/api/health')
        self.assertEqual(response.status_code, 503, str(response.json()))

    async def test_first_run_cap_is_respected(self):
        videos = [{'bvid': f'BVcap{i}', 'title': 'test', 'created': 1788912000, 'length': 10} for i in range(3)]
        c = SimpleNamespace(get_video_list=AsyncMock(side_effect=[{'list': {'vlist': videos}}, {'list': {'vlist': []}}]), get_dynamics=AsyncMock(return_value={'items': []}))
        processed = AsyncMock()
        with patch.object(fetcher, '_process_video', processed), patch.object(fetcher.asyncio, 'sleep', AsyncMock()):
            await fetcher._fetch_blogger(c, 1, 'Review')
        self.assertLessEqual(processed.await_count, 2)

    async def test_comment_total_preserved(self):
        async with bilibili.BiliClient() as client:
            with patch.object(client, '_get', AsyncMock(return_value={'code': 0, 'data': {'replies': [{'content': {'message': 'test'}}], 'page': {'count': 500}}})):
                data = await client.get_comments(1)
        self.assertEqual(data['total'], 500)
        self.assertEqual(len(data['replies']), 1)

    async def test_invalid_batch_mid_is_400(self):
        response = await self.http.post('/api/bloggers/sync/add', params={'mids': 'bad'})
        self.assertEqual(response.status_code, 400)

    async def test_array_llm_output_is_failure(self):
        with patch.object(analyzer, '_call_llm', AsyncMock(return_value='[]')):
            data = await analyzer.analyze_video('test', 'test', [], [], 10)
        self.assertTrue(data['analysis_failed'])

    async def test_cc_exception_falls_back_to_ai(self):
        c = SimpleNamespace(get_cc_subtitle=AsyncMock(side_effect=TimeoutError('test')), get_subtitle_url=AsyncMock(return_value='mock://subtitle'))
        with patch.object(transcript, '_download_subtitle', AsyncMock(return_value='a' * 60)):
            data = await transcript.fetch_transcript(c, 'BVtest', 1, 10)
        self.assertEqual(data['source'], 'ai_subtitle')

    async def test_valid_curl_video_response_is_parsed(self):
        proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"code":0,"data":{"cid":123}}\n200', b'')))
        async with bilibili.BiliClient() as client:
            with patch.object(bilibili.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)), patch.object(bilibili.asyncio, 'sleep', AsyncMock()):
                data = await client.get_video_info('BVmock')
        self.assertEqual(data.get('cid'), 123)

    async def test_valid_curl_dynamic_response_is_parsed(self):
        proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"code":0,"data":{"items":[{"id_str":"123"}]}}\n200', b'')))
        async with bilibili.BiliClient() as client:
            client._buvid3 = 'TEST_ONLY'
            with patch.object(bilibili.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)):
                data = await client.get_dynamics(1)
        self.assertEqual(len(data.get('items', [])), 1)

    async def test_partial_asr_is_not_reported_complete(self):
        c = SimpleNamespace(get_audio_url=AsyncMock(return_value='mock://audio'), download_audio=AsyncMock(return_value=b'12345678'))
        with patch.object(transcript, '_split_audio', AsyncMock(return_value=[b'part1', b'part2'])), patch.object(transcript, '_call_asr', AsyncMock(side_effect=['first half only', None])):
            with self.assertRaisesRegex(ValueError, '2/2 failed'):
                await transcript._asr_transcribe(c, 'BVpartial', 1, 360)

    async def test_analysis_validator_rejects_invalid_types_and_range(self):
        self.assertFalse(analyzer._validate_analysis({'summary': None, 'key_points': 'wrong type', 'sentiment': 'neutral', 'sentiment_score': 999}))

    async def test_curl_timeout_kills_and_reaps_process(self):
        killed = []
        async def communicate():
            await asyncio.sleep(1)
            return b'', b''
        proc = SimpleNamespace(communicate=communicate, kill=lambda: killed.append(True), wait=AsyncMock(return_value=0))
        with patch.object(bilibili.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertRaises(TimeoutError):
                await bilibili._run_curl(['curl', 'mock://timeout'], timeout=0.01)
        self.assertEqual(killed, [True])
        proc.wait.assert_awaited_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
