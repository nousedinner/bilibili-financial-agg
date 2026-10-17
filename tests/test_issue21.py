"""Offline failure attribution and paid-retry authorization regression coverage."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch

for key, value in {'DB_USER': 'review', 'DB_PASSWORD': 'review', 'DB_HOST': '127.0.0.1',
                   'XIAOMI_API_KEY': 'TEST_ONLY', 'SESSDATA': ''}.items():
    os.environ[key] = value

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src import analyzer, main, workflow, fetcher, migrations, config, collection, pipeline, authorization, ownership, lifecycle, auth_routes
from src.credentials import credential_store
from src.lifecycle import WorkflowError, owner_context
from src.models import WorkStage, VideoWork, PaidRetryReset, DailyUsage, Summary, Base, Blogger


class Issue21(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_config = copy.deepcopy(config._CONFIG)
        self.temp = tempfile.TemporaryDirectory(prefix='fin-issue21-')
        config._CONFIG = {'limits': {'request_interval_seconds': 0}, 'data': {
            'credential_path': str(Path(self.temp.name) / 'credential.json'),
            'outbox_path': str(Path(self.temp.name) / 'outbox.db')}}
        self.at = datetime(2026, 9, 10, 12)
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch.object(main, 'async_session', self.sessions), patch.object(fetcher, 'async_session', self.sessions)]
        self.patches += [patch.object(module, 'now', lambda: self.at)
            for module in (workflow, collection, pipeline, authorization, ownership, lifecycle, auth_routes)]
        for item in self.patches:
            item.start()
        main.app.dependency_overrides[main.require_api_key] = lambda: None
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://fixture.invalid')

    async def asyncTearDown(self):
        await self.http.aclose()
        main.app.dependency_overrides.clear()
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        fetcher.outbox_close()
        self.temp.cleanup()
        config._CONFIG = self.previous_config

    def payload(self, bvid='BVnew'):
        from zoneinfo import ZoneInfo
        return {'bvid': bvid, 'title': 'fixture', 'created': (self.at-timedelta(hours=1)).replace(
            tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(), 'length': 7, 'aid': 123}

    def client(self):
        return SimpleNamespace(credential_version=1,
            get_video_info=AsyncMock(return_value={'cid':77,'duration':7}),
            get_comments=AsyncMock(return_value={'replies':[],'total':0}),
            get_danmaku=AsyncMock(return_value={'items':[],'total':0}),
            get_video_aid=AsyncMock(return_value=123))

    async def admit(self, bvid='BVnew', origin='incremental'):
        async with self.sessions() as session:
            if await session.get(Blogger,1) is None:
                session.add(Blogger(mid=1,name='fixture',enabled=True))
                await session.commit()
        batch = await workflow.create_batch(self.sessions, origin=origin,
            since=workflow.HARD_SINCE if origin=='manual_backfill' else lifecycle.automatic_floor(self.at),
            until=self.at, max_videos=5, max_pages=5, mid=1)
        await workflow.admit(self.sessions,batch,1,self.payload(bvid),allow_legacy=origin=='manual_backfill')
        await workflow.freeze_batch(self.sessions,batch)
        return batch

    async def exhausted(self, stage='analysis'):
        await self.admit('BV1234567890')
        async with self.sessions() as session:
            work = await session.get(VideoWork, 'BV1234567890')
            work.state, work.reason = 'exhausted', 'llm_failed'
            row = await session.get(WorkStage, ('BV1234567890', stage))
            if row is None:
                row = WorkStage(bvid=work.bvid, stage=stage)
                session.add(row)
            row.state, row.attempts, row.reason = 'failed', 3, 'llm_failed'
            row.result = {'failure': {'kind': 'json_parse_failed'}}
            session.add(DailyUsage(day=self.at.date(), llm_calls=27, audio_seconds=42))
            await session.commit()

    def request(self, **kwargs):
        return dict(request_id=str(uuid4()), bvid='BV1234567890', stage='analysis', reason='operator verified upstream recovery', **kwargs)

    async def test_reset_changes_only_failed_stage_and_preserves_budget_and_evidence(self):
        await self.exhausted()
        request = self.request()
        first = await workflow.reset_paid_stage(self.sessions, **request)
        self.assertFalse(first['replayed'])
        async with self.sessions() as session:
            row = await session.get(WorkStage, ('BV1234567890', 'analysis'))
            self.assertEqual((row.state, row.attempts), ('pending', 0))
            self.assertIn('failure', row.result)
            other = await session.get(WorkStage, ('BV1234567890', 'metadata'))
            self.assertEqual((other.state, other.attempts), ('pending', 0))
            budget = await session.get(DailyUsage, self.at.date())
            self.assertEqual((budget.llm_calls, budget.audio_seconds), (27, 42))
            receipt = await session.get(PaidRetryReset, request['request_id'])
            self.assertEqual(receipt.previous['attempts'], 3)
            row.attempts, row.state = 2, 'failed'
            await session.commit()
        self.assertTrue((await workflow.reset_paid_stage(self.sessions, **request))['replayed'])
        async with self.sessions() as session:
            self.assertEqual((await session.get(WorkStage, ('BV1234567890', 'analysis'))).attempts, 2)

    async def test_reset_request_id_cannot_be_repurposed(self):
        await self.exhausted()
        request = self.request()
        await workflow.reset_paid_stage(self.sessions, **request)
        request['reason'] = 'different command'
        with self.assertRaisesRegex(WorkflowError, 'reset_request_conflict'):
            await workflow.reset_paid_stage(self.sessions, **request)

    async def test_reset_refuses_scope_expiry_running_and_disabled_blogger(self):
        from src.models import Blogger
        await self.exhausted()
        for case, expected in [('running', 'task_running'), ('expired', 'scope_expired'), ('disabled', 'blogger_disabled')]:
            async with self.sessions() as session:
                work = await session.get(VideoWork, 'BV1234567890')
                work.state = 'running' if case == 'running' else 'exhausted'
                work.expires_at = self.at - timedelta(seconds=1) if case == 'expired' else self.at + timedelta(days=1)
                (await session.get(Blogger, 1)).enabled = case != 'disabled'
                await session.commit()
            with self.assertRaisesRegex(WorkflowError, expected):
                await workflow.reset_paid_stage(self.sessions, **self.request())

    async def test_reset_refuses_successful_nonpaid_and_nonexhausted_stages(self):
        await self.exhausted()
        with self.assertRaisesRegex(WorkflowError, 'invalid_paid_stage'):
            request = self.request(); request['stage'] = 'asr'
            await workflow.reset_paid_stage(self.sessions, **request)
        for state, attempts in [('complete', 3), ('failed', 2)]:
            async with self.sessions() as session:
                row = await session.get(WorkStage, ('BV1234567890', 'analysis'))
                row.state, row.attempts = state, attempts
                await session.commit()
            with self.assertRaisesRegex(WorkflowError, 'paid_stage_not_exhausted'):
                await workflow.reset_paid_stage(self.sessions, **self.request())

    async def test_reset_endpoint_requires_key_and_respects_job_mutex(self):
        await self.exhausted()
        request = self.request()
        main.app.dependency_overrides.clear()
        self.assertEqual((await self.http.post('/api/retry/reset-paid', json=request)).status_code, 401)
        self.assertEqual((await self.http.post('/api/retry/reset-paid', json=request, headers={'X-API-Key': 'wrong'})).status_code, 401)
        main.app.dependency_overrides[main.require_api_key] = lambda: None
        with patch.object(main, '_job_lock', asyncio.Lock()):
            await main._job_lock.acquire()
            self.assertEqual((await self.http.post('/api/retry/reset-paid', json=request)).status_code, 409)
            main._job_lock.release()
            response = await self.http.post('/api/retry/reset-paid', json=request)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(main._job_lock.locked())
            response = await self.http.post('/api/retry/reset-paid', json=request)
            self.assertTrue(response.json()['data']['replayed'])
            request['bvid'] = 'invalid'
            self.assertEqual((await self.http.post('/api/retry/reset-paid', json=request)).status_code, 422)

    async def test_stale_owner_cannot_reset(self):
        await self.exhausted()
        from src.lifecycle import OwnershipLost
        class Stale:
            async def fence(self, session):
                raise OwnershipLost()
        token = owner_context.set(Stale())
        try:
            with self.assertRaises(OwnershipLost):
                await workflow.reset_paid_stage(self.sessions, **self.request())
        finally:
            owner_context.reset(token)
        async with self.sessions() as session:
            self.assertEqual((await session.get(WorkStage, ('BV1234567890', 'analysis'))).attempts, 3)

    async def test_long_part_counters_survive_changed_input_and_manual_readmission(self):
        await self.exhausted('analysis_part_0')
        async with self.sessions() as session:
            row = await session.get(WorkStage, ('BV1234567890', 'analysis_part_0'))
            row.input_version = 'old'
            await session.commit()
        await self.admit('BV1234567890', origin='manual_backfill')
        context = workflow.WorkContext(self.sessions, 'BV1234567890', 1)
        with self.assertRaisesRegex(WorkflowError, 'retry_exhausted'):
            await context.stage('analysis_part_0', 'running', input_version='new')
        request = self.request(); request['stage'] = 'analysis_part_0'
        async with self.sessions() as session:
            row = await session.get(WorkStage, ('BV1234567890', 'analysis_part_0'))
            row.state, row.reason = 'failed', 'retry_exhausted'
            await session.commit()
        await workflow.reset_paid_stage(self.sessions, **request)

    async def test_model_failures_have_specific_transport_causes(self):
        request = httpx.Request('POST', 'https://llm.invalid/')
        for exception, cause in [(httpx.ReadTimeout('fixture'), 'http_timeout'),
                (httpx.ConnectError('fixture'), 'network_error'),
                (httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(429, request=request)), 'http_429'),
                (httpx.HTTPStatusError('fixture', request=request, response=httpx.Response(503, request=request)), 'http_503')]:
            with patch.object(httpx.AsyncClient, 'post', AsyncMock(side_effect=exception)):
                result = await analyzer.analyze_video('fixture', 'text', [], [], 7)
            self.assertEqual(result['_failure']['kind'], 'llm_call_failed')
            self.assertEqual(result['_failure']['cause'], cause)

    async def test_missing_key_empty_and_invalid_api_responses(self):
        with patch.object(analyzer, 'get_env', return_value=''):
            result = await analyzer.analyze_video('fixture', 'text', [], [], 7)
        self.assertEqual(result['_failure']['cause'], 'missing_api_key')
        for payload, cause in [({'choices': []}, 'empty_response'), ([], 'invalid_api_response')]:
            response = httpx.Response(200, request=httpx.Request('POST', 'https://llm.invalid'), json=payload)
            with patch.object(httpx.AsyncClient, 'post', AsyncMock(return_value=response)):
                result = await analyzer.analyze_video('fixture', 'text', [], [], 7)
            self.assertEqual(result['_failure']['cause'], cause)

    async def test_truncated_output_records_finish_reason_and_bounded_raw(self):
        response = httpx.Response(200, request=httpx.Request('POST', 'https://llm.invalid'),
            json={'choices': [{'finish_reason': 'length', 'message': {'content': '{"summary":"' + 'x'*2000}}]})
        with patch.object(httpx.AsyncClient, 'post', AsyncMock(return_value=response)):
            result = await analyzer.analyze_video('fixture', 'text', [], [], 7)
        self.assertEqual(result['_failure']['kind'], 'json_parse_failed')
        self.assertEqual(result['_failure']['finish_reason'], 'length')
        self.assertEqual(len(result['_failure']['raw_head']), 1200)

    async def test_interaction_checks_do_not_require_body_fields(self):
        with patch.object(analyzer, '_call_llm', AsyncMock(return_value='{"comment_sentiment": {"bullish": 0, "bearish": 0, "neutral": 0}, "comment_keywords": []}')):
            result = await analyzer.analyze_interactions([{'content': {'message': 'sample'}}], [])
        self.assertEqual(result['_failure']['kind'], 'validation_failed')
        self.assertTrue(any('comment_sentiment_sum' in c for c in result['_failure']['checks']))
        self.assertFalse(any('summary' in c for c in result['_failure']['checks']))

    async def test_interaction_failure_detail_survives_pipeline_rethrow(self):
        await self.admit()
        client = self.client()
        good = analyzer._empty_analysis(); good['summary'] = 'verified fixture'
        with patch.object(fetcher, 'fetch_transcript', AsyncMock(return_value={'source': 'cc_subtitle', 'text': 'fixture', 'segments': 1})), \
                patch.object(fetcher, 'analyze_video', AsyncMock(return_value=good)):
            await fetcher._process_video(client, 1, self.payload())
        async with self.sessions() as session:
            stage = await session.get(WorkStage, ('BVnew', 'interaction_analysis'))
            stage.state = 'failed'
            await session.commit()
        detail = {'kind': 'validation_failed', 'checks': ['comment_sentiment_sum=0.000']}
        with patch.object(analyzer, 'analyze_interactions', AsyncMock(return_value={'analysis_failed': True, '_failure': detail})):
            with self.assertRaises(WorkflowError) as raised:
                await fetcher._process_video(client, 1, self.payload())
        self.assertEqual(raised.exception.detail, detail)
        async with self.sessions() as session:
            stage = await session.get(WorkStage, ('BVnew', 'interaction_analysis'))
            self.assertEqual(stage.result['failure'], detail)
            self.assertEqual((await session.get(Summary, 'BVnew')).summary, good['summary'])

    async def test_sqlite_migration_remains_idempotent_and_does_not_admit_history(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(migrations.upgrade)
            await connection.run_sync(migrations.upgrade)
            await connection.run_sync(migrations.verify)
        async with self.sessions() as session:
            self.assertEqual((await session.execute(select(func.count()).select_from(VideoWork))).scalar(), 0)
        for table in Base.metadata.tables.values():
            self.assertEqual(table.dialect_options['mysql']['collate'], 'utf8mb4_unicode_ci')
