"""Business regressions use an isolated database and mocked remote services."""
import asyncio
import io
import json
import shutil
import subprocess
import unittest
import wave
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from tests import test_acceptance as baseline
from src import main, fetcher, analyzer, bilibili, transcript, migrations, db, config
from src.models import *
from sqlalchemy import select, text
from sqlalchemy.dialects import mysql


def analysis():
    data = analyzer._empty_analysis()
    data['summary'] = 'Complete verified analysis'
    return data


def digest():
    return {'summary': 'Complete digest', 'overall_sentiment': 'neutral', 'consensus': [],
        'differences': [], 'key_topics': [], 'overall_sentiment_desc': 'Balanced'}


class Regressions(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = baseline.Acceptance.asyncSetUp
    asyncTearDown = baseline.Acceptance.asyncTearDown
    client = baseline.Acceptance.client
    seed_videos = baseline.Acceptance.seed_videos

    async def test_mixed_same_second_pages_have_no_duplicates_or_gaps(self):
        await self.seed_videos(tied=True)
        async with self.sessions() as session:
            session.add_all([Dynamic(dyn_id=str(i), mid=1, publish_time=datetime(2026, 9, 9, 12)) for i in range(4)])
            await session.commit()
        params, ids = {'limit': 2}, []
        for _ in range(5):
            response = await self.http.get('/api/feed', params=params)
            self.assertEqual(response.status_code, 200)
            data = response.json()['data']
            self.assertEqual(data['total'], 7)
            ids.extend((item['type'], item.get('bvid') or item.get('dyn_id')) for item in data['items'])
            if not data['has_more']:
                break
            params.update(data['next_cursor'])
        self.assertEqual(len(ids), 7)
        self.assertEqual(len(set(ids)), 7)

    async def test_filters_apply_before_count_and_pagination(self):
        await self.seed_videos()
        async with self.sessions() as session:
            session.add_all([Summary(bvid='BVreview0', tags=['other'], sentiment='bullish'),
                Summary(bvid='BVreview2', tags=['target'], sentiment='bearish')])
            await session.commit()
        data = (await self.http.get('/api/videos', params={'tag': 'target', 'sentiment': 'bearish', 'limit': 1})).json()['data']
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['bvid'], 'BVreview2')
        self.assertFalse(data['has_more'])
        feed = (await self.http.get('/api/feed', params={'blogger': 2})).json()['data']
        self.assertEqual(feed['items'], [])

    async def test_structured_batch_preserves_commas_and_pairing(self):
        response = await self.http.post('/api/bloggers/sync/add', json=[{'mid': 3, 'name': 'First, Last'}, {'mid': 4, 'name': 'Other'}])
        self.assertEqual(response.status_code, 200, response.text)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Blogger, 3)).name, 'First, Last')
            self.assertEqual((await session.get(Blogger, 4)).name, 'Other')

    async def test_legacy_empty_name_does_not_shift_following_name(self):
        response = await self.http.post('/api/bloggers/sync/add', params={'mids': '3,4', 'names': ',Second'})
        self.assertEqual(response.status_code, 200)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Blogger, 4)).name, 'Second')
            self.assertEqual((await session.get(Blogger, 3)).name, '用户3')

    async def test_bad_inputs_rejected_before_job_reservation(self):
        for body in ({'since': 'bad'}, {'since': '2026-01-01', 'mid': -1}):
            response = await self.http.post('/api/fetch/backfill', json=body)
            self.assertEqual(response.status_code, 422)
        self.assertEqual((await self.http.post('/api/users', json={'username': 'x' * 51})).status_code, 422)
        self.assertEqual((await self.http.get('/api/feed', params={'before': 'nan'})).status_code, 422)
        self.assertFalse(main._job_lock.locked())

    async def test_video_success_updates_all_related_tables_and_dirty_date(self):
        c = self.client()
        c.get_comments.return_value = {'replies': [{'content': {'message': 'hello'}}], 'total': 99}
        c.get_danmaku.return_value = {'items': ['one'], 'total': 501}
        with patch.object(fetcher, 'fetch_transcript', AsyncMock(return_value={'text': 'complete transcript', 'source': 'asr', 'segments': 2})), patch.object(fetcher, 'analyze_video', AsyncMock(return_value=analysis())):
            await fetcher._process_video(c, 1, {'bvid': 'BVsuccess', 'created': 1788912000, 'length': 10, 'aid': 123})
        async with self.sessions() as session:
            self.assertEqual((await session.get(Video, 'BVsuccess')).fetch_status, 'ok')
            self.assertEqual((await session.get(Transcript, 'BVsuccess')).segment_count, 2)
            self.assertEqual((await session.get(Summary, 'BVsuccess')).summary, analysis()['summary'])
            self.assertEqual((await session.get(CommentAnalysis, ('BVsuccess', 'video'))).total_count, 99)
            self.assertEqual((await session.get(DanmakuAnalysis, 'BVsuccess')).total_count, 501)
            self.assertEqual((await session.get(DanmakuAnalysis, 'BVsuccess')).sampled_count, 1)
            self.assertTrue((await session.execute(select(DirtyDigest))).scalars().all())

    async def test_retry_failure_does_not_commit_transcript_without_analysis(self):
        async with self.sessions() as session:
            session.add(Blogger(mid=1, name='Enabled'))
            session.add(Video(bvid='BVretry', mid=1, fetch_status='ok', publish_time=datetime(2026, 9, 8, 12)))
            session.add(Summary(bvid='BVretry', summary='old'))
            await session.commit()
        @asynccontextmanager
        async def client(): yield self.client()
        with patch.object(fetcher, '_bili_client', client), patch.object(fetcher, 'fetch_transcript', AsyncMock(return_value={'text': 'new transcript', 'source': 'asr', 'segments': 1})), patch.object(fetcher, 'analyze_video', AsyncMock(return_value=analyzer._empty_analysis(True))):
            result = await main._retry_work()
        self.assertEqual(result['failed'], 1)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Transcript, 'BVretry'))
            self.assertEqual((await session.get(Summary, 'BVretry')).summary, 'old')
            self.assertEqual((await session.get(Video, 'BVretry')).fetch_status, 'failed')

    async def test_cancellation_is_persisted_and_retryable(self):
        c = self.client()
        c.get_video_info.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await fetcher._process_video(c, 1, {'bvid': 'BVcancel', 'created': 1785513600})
        async with self.sessions() as session:
            self.assertEqual((await session.get(Video, 'BVcancel')).fetch_status, 'failed')

    async def test_dynamic_failure_then_success_removes_pending_queue(self):
        data = {'id_str': '456', 'modules': {'module_dynamic': {'desc': {'text': 'Content'}}, 'module_author': {'pub_ts': 1788912000}}}
        with patch.object(fetcher, 'analyze_dynamic', AsyncMock(side_effect=[{'analysis_failed': True}, {'summary': 'Good', 'sentiment': 'neutral', 'tags': []}])):
            with self.assertRaises(ValueError): await fetcher._process_dynamic(None, 1, data)
            await fetcher._process_dynamic(None, 1, data)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(PendingDynamic, '456'))
            self.assertEqual((await session.get(Dynamic, '456')).summary, 'Good')
            self.assertTrue((await session.execute(select(DirtyDigest))).scalars().all())

    async def test_dynamic_pagination_uses_offset_and_keeps_failed_payload(self):
        def item(id): return {'id_str': id, 'modules': {'module_dynamic': {'desc': {'text': id}}}}
        c = SimpleNamespace(get_video_list=AsyncMock(return_value={'list': {'vlist': []}}),
            get_dynamics=AsyncMock(side_effect=[{'items': [item('20')], 'has_more': True, 'offset': 'next'},
                {'items': [item('19')], 'has_more': False}]))
        with patch.object(fetcher, 'analyze_dynamic', AsyncMock(return_value={'analysis_failed': True})):
            result = await fetcher._fetch_blogger(c, 1, 'Test')
        self.assertEqual(result['failed_count'], 2)
        self.assertEqual(c.get_dynamics.await_args_list[1].kwargs['offset'], 'next')
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(PendingDynamic, '19'))
            self.assertEqual((await session.get(FetchWatermark, 1)).last_dyn_id, '20')

    async def test_incremental_scan_continues_beyond_ten_pages(self):
        async with self.sessions() as session:
            session.add(FetchWatermark(mid=1, last_bvid='BVold'))
            await session.commit()
        pages = [{'list': {'vlist': [{'bvid': f'BV{i}', 'created': 1788912000}]}} for i in range(11)]
        pages.append({'list': {'vlist': [{'bvid': 'BVold', 'created': 1788912000}]}})
        c = SimpleNamespace(get_video_list=AsyncMock(side_effect=pages), get_dynamics=AsyncMock(return_value={'items': []}))
        # Issue #11 #1: cap now applies to incremental scans too; raise cap for this test
        original = config._CONFIG
        try:
            config._CONFIG = {**original, 'data': {**original.get('data', {}), 'first_run_cap': 20}}
            with patch.object(fetcher, '_process_video', AsyncMock()) as process:
                await fetcher._fetch_blogger(c, 1, 'Test')
        finally:
            config._CONFIG = original
        self.assertEqual(process.await_count, 11)
        self.assertEqual(c.get_video_list.await_count, 12)

    async def test_repeated_video_page_does_not_move_watermark(self):
        async with self.sessions() as session:
            session.add(FetchWatermark(mid=1, last_bvid='BVold'))
            await session.commit()
        c = SimpleNamespace(get_video_list=AsyncMock(return_value={'list': {'vlist': [{'bvid': 'BVnew', 'created': 1788912000}]}}))
        with self.assertRaisesRegex(ValueError, 'repeated'):
            await fetcher._fetch_blogger(c, 1, 'Test')
        async with self.sessions() as session:
            self.assertEqual((await session.get(FetchWatermark, 1)).last_bvid, 'BVold')

    async def test_backfill_cap_counts_failed_attempts(self):
        async with self.sessions() as session:
            session.add(Blogger(mid=42, name='Test'))
            await session.commit()
        pages = {'list': {'vlist': [{'bvid': f'BV{i}', 'created': 1788912000} for i in range(5)]}}
        with patch.object(bilibili.BiliClient, 'get_video_list', AsyncMock(return_value=pages)), patch.object(fetcher, '_process_video', AsyncMock(side_effect=ValueError('failure'))) as process:
            result = await fetcher.run_backfill(42, '2026-01-01', cap=2)
        self.assertEqual(result['failed'], 2)
        self.assertEqual(process.await_count, 2)

    async def test_late_content_rebuilds_original_day_and_keeps_failed_day_pending(self):
        day = fetcher._latest_closed_date() - timedelta(days=2)
        async with self.sessions() as session:
            session.add(Blogger(mid=1, name='Test'))
            session.add(Dynamic(dyn_id='late', mid=1, summary='Late content', sentiment='neutral', publish_time=datetime.combine(day, datetime.min.time()).replace(hour=12)))
            session.add(DirtyDigest(digest_date=day))
            session.add(DailyDigest(digest_date=day, content={'summary': 'Old content'}))
            await session.commit()
        with patch.object(fetcher, 'generate_daily_digest', AsyncMock(return_value={'analysis_failed': True})):
            self.assertEqual((await fetcher.regenerate_dirty_digests())['failed'], 1)
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(DirtyDigest, day))
            self.assertEqual((await session.get(DailyDigest, day)).content['summary'], 'Old content')
        with patch.object(fetcher, 'generate_daily_digest', AsyncMock(return_value=digest())):
            self.assertEqual((await fetcher.regenerate_dirty_digests())['generated'], 1)
        async with self.sessions() as session:
            self.assertIsNone(await session.get(DirtyDigest, day))
            self.assertEqual((await session.get(DailyDigest, day)).content['summary'], 'Complete digest')

    async def test_cutoff_boundary_and_latest_closed_day(self):
        async with self.sessions() as session:
            await fetcher._mark_digest_dirty(session, datetime(2026, 9, 8, 21, 59))
            await session.flush()
            await fetcher._mark_digest_dirty(session, datetime(2026, 9, 8, 22))
            await session.commit()
            dates = set((await session.execute(select(DirtyDigest.digest_date))).scalars().all())
        self.assertEqual(dates, {date(2026, 9, 8), date(2026, 9, 9)})
        with patch.object(fetcher, '_utcnow', return_value=datetime(2026, 9, 9, 21)):
            self.assertEqual(fetcher._latest_closed_date(), date(2026, 9, 8))

    async def test_job_reservation_reports_busy_and_persists_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def work():
            entered.set(); await release.wait(); return {'total_failed': 1}
        with patch.object(main, '_fetch_work', work):
            first = await self.http.post('/api/fetch/trigger')
            await entered.wait()
            second = await self.http.post('/api/transcripts/retry')
            self.assertEqual(second.status_code, 409)
            job_id = first.json()['data']['id']
            running = (await self.http.get(f'/api/tasks/{job_id}')).json()['data']
            self.assertEqual(running['status'], 'running')
            tasks = list(main._background_tasks)
            release.set()
            await asyncio.gather(*tasks)
        finished = (await self.http.get(f'/api/tasks/{job_id}')).json()['data']
        self.assertEqual(finished['status'], 'partial')
        self.assertIsNotNone(finished['finished_at'])

    async def test_startup_recovers_interrupted_jobs_and_pending_videos(self):
        async with self.sessions() as session:
            session.add(FetchJob(id='old', kind='fetch', status='running'))
            session.add(Video(bvid='BVold', mid=1, fetch_status='pending'))
            await session.commit()
        await main._recover_interrupted()
        async with self.sessions() as session:
            self.assertEqual((await session.get(FetchJob, 'old')).status, 'interrupted')
            self.assertEqual((await session.get(Video, 'BVold')).fetch_status, 'failed')

    async def test_followings_failure_never_returns_partial_success(self):
        async with bilibili.BiliClient() as client:
            with patch.object(client, '_get', AsyncMock(side_effect=[{'data': {'list': [{'mid': i} for i in range(50)], 'total': 70}}, bilibili.BiliAPIError(503, 'failed')])):
                with self.assertRaises(bilibili.BiliAPIError): await client.get_followings(1, ps=200)

    async def test_disabled_following_is_offered_for_reenable(self):
        async with self.sessions() as session:
            session.add(Blogger(mid=3, name='Disabled', enabled=False))
            await session.commit()
        with patch.object(main, 'get_env', side_effect=lambda key: '123' if key == 'BILI_UID' else 'TEST_ONLY'), patch.object(bilibili.BiliClient, 'get_followings', AsyncMock(return_value=[{'mid': 3, 'uname': 'Disabled'}])):
            response = await self.http.post('/api/bloggers/sync')
        self.assertEqual(response.json()['data']['new'][0]['mid'], 3)

    async def test_wbi_invalid_keys_and_signed_values(self):
        async with bilibili.BiliClient() as client:
            with self.assertRaises(bilibili.BiliAPIError): client._get_mixin_key('')
            client._img_key, client._sub_key = 'a' * 32, 'b' * 32
            with patch.object(client, '_refresh_wbi_keys', AsyncMock()):
                params = await client.sign_params({'keyword': "a'b(c)*!"})
            self.assertEqual(params['keyword'], 'abc')

    async def test_video_list_internal_retries_cannot_exceed_page_budget(self):
        budget = {'pages': 1, 'new_videos': 0, 'retries': 0}
        async with bilibili.BiliClient() as client:
            with patch.object(client, 'sign_params', AsyncMock(side_effect=lambda params: params)), \
                    patch.object(bilibili, '_run_curl', AsyncMock(side_effect=bilibili.BiliAPIError(-1, 'outage'))) as request, \
                    patch.object(bilibili.asyncio, 'sleep', AsyncMock()):
                with self.assertRaisesRegex(bilibili.BiliAPIError, 'budget exhausted'):
                    await fetcher._fetch_blogger(client, 1, 'Test', budget=budget)
        self.assertEqual(request.await_count, 1)
        self.assertEqual(budget['pages'], 0)

    async def test_video_list_retries_consume_each_remaining_page_token(self):
        budget = {'pages': 3, 'new_videos': 0, 'retries': 0}
        response = json.dumps({
            'code': 0,
            'data': {'list': {'vlist': []}, 'page': {}},
        }).encode()
        async with bilibili.BiliClient() as client:
            with patch.object(client, 'sign_params', AsyncMock(side_effect=lambda params: params)), \
                    patch.object(bilibili, '_run_curl', AsyncMock(side_effect=[
                        bilibili.BiliAPIError(-1, 'first'),
                        bilibili.BiliAPIError(-1, 'second'),
                        (response, 200),
                    ])) as request, patch.object(bilibili.asyncio, 'sleep', AsyncMock()):
                result = await fetcher._fetch_blogger(client, 1, 'Test', budget=budget)
        self.assertEqual(request.await_count, 3)
        self.assertEqual(budget['pages'], 0)
        self.assertFalse(result.get('has_more', False))

    async def test_exhausted_page_budget_marks_fetch_as_truncated(self):
        client = SimpleNamespace(
            validate_sessdata=AsyncMock(return_value={'valid': True}),
            get_video_list=AsyncMock(),
        )

        @asynccontextmanager
        async def client_context():
            yield client

        original = config._CONFIG
        try:
            config._CONFIG = {**original, 'data': {
                **original.get('data', {}),
                'task_page_budget': 0,
                'task_video_budget': 10,
                'task_retry_budget': 10,
            }}
            with patch.object(fetcher, '_get_enabled_bloggers', AsyncMock(return_value=[
                    {'mid': 1, 'name': 'Test', 'enabled': True},
            ])), patch.object(fetcher, '_bili_client', client_context):
                result = await fetcher.run_fetch_only()
        finally:
            config._CONFIG = original
        client.get_video_list.assert_not_awaited()
        self.assertTrue(result['truncated'])
        self.assertTrue(result['budget_exhausted'])

    async def test_curl_exit_failure_raises_and_cancellation_reaps(self):
        proc = SimpleNamespace(returncode=7, communicate=AsyncMock(return_value=(b'\n000', b'')))
        with patch.object(bilibili.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertRaises(bilibili.BiliAPIError): await bilibili._run_curl(['curl', 'mock://failure'])
        proc = SimpleNamespace(communicate=AsyncMock(side_effect=asyncio.CancelledError()), kill=lambda: None, wait=AsyncMock())
        with patch.object(bilibili.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertRaises(asyncio.CancelledError): await bilibili._run_curl(['curl', 'mock://cancel'])
        proc.wait.assert_awaited_once()

    async def test_llm_rejects_arrays_and_invalid_each_field(self):
        self.assertIsNone(analyzer._try_parse_json(json.dumps([analysis()])))
        for key, value in [('summary', None), ('key_points', 'text'), ('tags', [4]), ('sentiment_score', True),
                ('sentiment_score', float('nan')), ('sentiment_score', 999), ('comment_sentiment', {'neutral': 1})]:
            data = analysis(); data[key] = value
            self.assertFalse(analyzer._validate_analysis(data), (key, value))
        data = analysis(); data['sentiment'] = ' NEUTRAL '
        self.assertTrue(analyzer._validate_analysis(data))
        self.assertEqual(data['sentiment'], 'neutral')

    async def test_migration_upgrades_legacy_column_idempotently(self):
        async with self.engine.begin() as connection:
            await connection.execute(text('ALTER TABLE videos DROP COLUMN retry_count'))
            await connection.run_sync(migrations.upgrade)
            await connection.run_sync(migrations.upgrade)
            await connection.run_sync(migrations.verify)
        self.assertEqual(db.DATABASE_URL.password, 'review')
        url = db.DATABASE_URL.set(password='a@b:c/d?#%')
        from sqlalchemy.engine import make_url
        self.assertEqual(make_url(url.render_as_string(hide_password=False)).password, 'a@b:c/d?#%')

    async def test_cron_honors_month_day_and_weekday(self):
        cfg = {'scheduler': {'fetch_cron_1': '5 12 2 10 *', 'fetch_cron_2': '0 21 * * mon', 'daily_cron': '10 22 * * *'}}
        with patch.object(main, 'get_config', return_value=cfg):
            try:
                main._start_scheduler()
                cron = str(main._scheduler.get_job('fetch_1').trigger)
                self.assertIn("month='10'", cron)
                self.assertIn("day='2'", cron)
                self.assertIn("day_of_week='mon'", str(main._scheduler.get_job('fetch_2').trigger))
            finally:
                main._scheduler.shutdown(wait=False)
                main._scheduler = None

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required for media integration tests')
    async def test_real_audio_time_segments_are_independently_decodable(self):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b'\0\0' * (16000 * 3))
        chunks = await transcript._split_audio(audio.getvalue(), 1)
        self.assertGreaterEqual(len(chunks), 3)
        sample_count = 0
        for chunk in chunks:
            proc = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
                '-f', 's16le', '-ar', '16000', '-ac', '1', 'pipe:1', stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            pcm, error = await asyncio.wait_for(proc.communicate(chunk), 20)
            self.assertEqual(proc.returncode, 0, error)
            self.assertTrue(pcm)
            sample_count += len(pcm) // 2
        self.assertGreaterEqual(sample_count, 16000 * 3)
        self.assertLessEqual(sample_count, 16000 * 4)

    # ── Issue #14 回归测试 ──

    async def test_watermark_not_advanced_when_video_budget_truncates(self):
        """Fix #1: 新视频预算截断时不推进水位线"""
        async with self.sessions() as session:
            session.add(FetchWatermark(mid=1, last_bvid='BVold'))
            await session.commit()
        # 3个新视频，预算只能处理2个
        pages = [{'list': {'vlist': [
            {'bvid': 'BV1', 'created': 1788912000},
            {'bvid': 'BV2', 'created': 1788911000},
            {'bvid': 'BV3', 'created': 1788910000},
        ]}},
            {'list': {'vlist': [{'bvid': 'BVold', 'created': 1788909000}]}},
        ]
        c = SimpleNamespace(get_video_list=AsyncMock(side_effect=pages))
        budget = {'pages': 5, 'new_videos': 2, 'retries': 0}
        with patch.object(config, '_CONFIG', {**config._CONFIG, 'data': {**config._CONFIG.get('data', {}), 'task_page_budget': 5, 'task_video_budget': 2, 'task_retry_budget': 0}}):
            with patch.object(fetcher, '_process_video', AsyncMock()) as process:
                result = await fetcher._fetch_blogger(c, 1, 'Test', budget=budget)
        # 预算2个，3个候选视频，只处理了2个
        self.assertEqual(process.await_count, 2)
        self.assertTrue(result.get('has_more'))
        # 水位线未推进
        async with self.sessions() as session:
            wm = await session.get(FetchWatermark, 1)
            self.assertEqual(wm.last_bvid, 'BVold')

    async def test_retry_candidates_are_sql_limited(self):
        """Fix #2: 重试候选SQL查询有LIMIT"""
        async with self.sessions() as session:
            session.add(Blogger(mid=1, name='Test'))
            # 创建15条失败视频
            for i in range(15):
                session.add(Video(bvid=f'BVfail{i}', mid=1, fetch_status='failed',
                    publish_time=datetime(2026, 9, 8, 12), error_type='transcript', retry_count=0))
            session.add(FetchWatermark(mid=1, last_bvid='BVold'))
            await session.commit()
        # retries=5，应只加载5+10=15条（刚好够），但如果有20条应该截断
        c = SimpleNamespace(get_video_list=AsyncMock(return_value={'list': {'vlist': [{'bvid': 'BVold', 'created': 1788912000}]}}))
        budget = {'pages': 2, 'new_videos': 0, 'retries': 5}
        with patch.object(config, '_CONFIG', {**config._CONFIG, 'data': {**config._CONFIG.get('data', {}), 'task_page_budget': 2, 'task_video_budget': 0, 'task_retry_budget': 5}}):
            with patch.object(fetcher, '_process_video', AsyncMock(side_effect=ValueError('mock'))) as process:
                result = await fetcher._fetch_blogger(c, 1, 'Test', budget=budget)
        # 应该只尝试重试5条（retries预算）
        self.assertEqual(process.await_count, 5)

    async def test_outbox_save_pop_pending(self):
        """Fix #3: SQLite outbox保存、移除和读取"""
        from src.fetcher import outbox_save, outbox_pop, outbox_pending
        import tempfile, os
        old_path = fetcher._outbox_path()
        with tempfile.TemporaryDirectory() as tmp:
            fetcher._outbox_path = lambda: os.path.join(tmp, 'test.db')
            fetcher._outbox_conn = None  # 重置连接
            try:
                outbox_save('job1', 'partial', {'total_new': 5}, None)
                pending = outbox_pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]['job_id'], 'job1')
                self.assertEqual(pending[0]['status'], 'partial')
                self.assertEqual(pending[0]['result']['total_new'], 5)
                outbox_pop('job1')
                self.assertEqual(len(outbox_pending()), 0)
            finally:
                fetcher._outbox_conn = None
                fetcher._outbox_path = lambda: old_path

    async def test_budget_exhaustion_sets_has_more(self):
        """Fix #4: 各维度预算耗尽时传播has_more"""
        # new_videos budget = 0，应立即设has_more
        async with self.sessions() as session:
            session.add(FetchWatermark(mid=1, last_bvid='BVold'))
            await session.commit()
        # 每个page需要不同的bvid避免pagination repeated
        pages = [{'list': {'vlist': [
            {'bvid': 'BV1', 'created': 1788912000},
        ]}}, {'list': {'vlist': [
            {'bvid': 'BV2', 'created': 1788911000},
        ]}}, {'list': {'vlist': [{'bvid': 'BVold', 'created': 1788909000}]}},
        ]
        c = SimpleNamespace(get_video_list=AsyncMock(side_effect=pages))
        budget = {'pages': 3, 'new_videos': 0, 'retries': 0}
        with patch.object(config, '_CONFIG', {**config._CONFIG, 'data': {**config._CONFIG.get('data', {}), 'task_page_budget': 3, 'task_video_budget': 0, 'task_retry_budget': 0}}):
            result = await fetcher._fetch_blogger(c, 1, 'Test', budget=budget)
        self.assertTrue(result.get('has_more'))

    async def test_regenerate_dirty_digests_reports_has_more(self):
        """Fix #6: 日报补偿超过max_per_round时报告has_more"""
        async with self.sessions() as session:
            # 创建8个dirty日期，超过max_per_round=5
            for i in range(8):
                day = date(2026, 9, 1) + timedelta(days=i)
                session.add(DirtyDigest(digest_date=day, retry_count=0))
            await session.commit()
        with patch.object(fetcher, 'generate_daily_digest', AsyncMock(return_value=digest())):
            result = await fetcher.regenerate_dirty_digests()
        self.assertTrue(result['has_more'])
        self.assertEqual(result['generated'], 5)  # 只处理5个
