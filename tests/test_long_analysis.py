"""Long-video recovery uses isolated storage and mocked model HTTP responses."""
import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select, func

from tests import test_issue21 as baseline
from src import analyzer, config, fetcher, main
from src.lifecycle import WorkflowError
from src.models import DailyUsage, PaidRetryReset, Summary, VideoWork, WorkStage


class LongAnalysis(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = baseline.Issue21.asyncSetUp
    asyncTearDown = baseline.Issue21.asyncTearDown
    admit = baseline.Issue21.admit
    payload = baseline.Issue21.payload
    client = baseline.Issue21.client
    request = baseline.Issue21.request
    bvid = 'BV1234567890'

    async def prepare(self):
        await self.admit(self.bvid)
        self.video = self.payload(self.bvid)
        self.upstream = self.client()
        self.transcript = {'source': 'cc_subtitle', 'text': 'a' * 6000 + 'b' * 3001, 'segments': 1}

    def response(self, valid=True, *, data=None):
        if data is None:
            data = analyzer._empty_analysis()
            data['summary'] = 'Verified fixture summary'
        return httpx.Response(200, request=httpx.Request('POST', 'https://model.invalid/'),
            json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(data) if valid else '{broken'}}]})

    async def process(self, responses):
        with patch.object(fetcher, 'fetch_transcript', AsyncMock(return_value=self.transcript)), \
                patch.object(httpx.AsyncClient, 'post', AsyncMock(side_effect=responses)) as model:
            try:
                result = await fetcher._process_video(self.upstream, 1, self.video)
            except WorkflowError as exc:
                result = exc
        self.at += timedelta(minutes=10)
        return result, model

    async def snapshot(self):
        async with self.sessions() as session:
            stages = (await session.execute(select(WorkStage).where(
                WorkStage.bvid == self.bvid))).scalars().all()
            usage = await session.get(DailyUsage, self.at.date())
            return {row.stage: row for row in stages}, usage

    async def reset(self, stage):
        request = self.request()
        request['stage'] = stage
        with patch.object(main, '_job_lock', asyncio.Lock()):
            response = await self.http.post('/api/retry/reset-paid', json=request)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()['data']['replayed'])
            replay = await self.http.post('/api/retry/reset-paid', json=request)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertTrue(replay.json()['data']['replayed'])

    async def test_failed_part_can_be_reset_and_published_without_repeating_successful_part(self):
        await self.prepare()
        for attempt in range(3):
            responses = ([self.response()] if attempt == 0 else []) + [self.response(False)]
            result, _ = await self.process(responses)
            self.assertEqual((result.reason, result.stage), ('llm_failed', 'analysis_part_1'))
        stages, usage = await self.snapshot()
        self.assertEqual((stages['analysis_part_0'].state, stages['analysis_part_0'].attempts), ('complete', 1))
        self.assertEqual((stages['analysis_part_1'].state, stages['analysis_part_1'].attempts), ('failed', 3))
        self.assertEqual((stages['analysis'].state, stages['analysis'].attempts), ('pending', 0))
        self.assertEqual(usage.llm_calls, 4)
        async with self.sessions() as session:
            self.assertEqual((await session.get(VideoWork, self.bvid)).state, 'exhausted')
            self.assertIsNone(await session.get(Summary, self.bvid))

        await self.reset('analysis_part_1')
        stages, usage = await self.snapshot()
        self.assertEqual(usage.llm_calls, 4)
        self.assertIn('failure', stages['analysis_part_1'].result)
        result, model = await self.process([self.response(), self.response()])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(model.await_count, 2)
        stages, usage = await self.snapshot()
        self.assertEqual((stages['analysis_part_0'].attempts, stages['analysis_part_1'].attempts,
                          stages['analysis'].attempts), (1, 1, 1))
        self.assertFalse(any(row.state == 'running' for row in stages.values()))
        self.assertEqual(usage.llm_calls, 6)
        async with self.sessions() as session:
            self.assertEqual((await session.get(VideoWork, self.bvid)).state, 'complete')
            self.assertEqual((await session.get(Summary, self.bvid)).summary, 'Verified fixture summary')
            self.assertEqual((await session.execute(select(func.count()).select_from(PaidRetryReset))).scalar(), 1)
            receipt = (await session.execute(select(PaidRetryReset))).scalar_one()
            self.assertEqual(receipt.previous['attempts'], 3)

    async def test_real_merge_attempts_still_exhaust_and_reset_preserves_parts(self):
        await self.prepare()
        for attempt in range(3):
            responses = ([self.response(), self.response()] if attempt == 0 else []) + [self.response(False)]
            result, _ = await self.process(responses)
            self.assertEqual((result.reason, result.stage), ('llm_failed', 'analysis'))
        stages, usage = await self.snapshot()
        self.assertEqual((stages['analysis'].state, stages['analysis'].attempts), ('failed', 3))
        self.assertEqual(usage.llm_calls, 5)
        with patch.object(analyzer, '_analyze_long_video', AsyncMock()) as parts:
            result, model = await self.process([])
        self.assertEqual((result.reason, result.stage), ('retry_exhausted', 'analysis'))
        parts.assert_not_awaited()
        model.assert_not_awaited()

        await self.reset('analysis')
        result, model = await self.process([self.response()])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(model.await_count, 1)
        stages, usage = await self.snapshot()
        self.assertEqual((stages['analysis_part_0'].attempts, stages['analysis_part_1'].attempts,
                          stages['analysis'].attempts), (1, 1, 1))
        self.assertEqual(usage.llm_calls, 6)

    async def test_parts_and_merge_still_share_the_daily_budget(self):
        await self.prepare()
        config._CONFIG['limits']['daily_llm_calls'] = 2
        result, model = await self.process([self.response(), self.response()])
        self.assertEqual((result.reason, result.stage), ('llm_budget', 'analysis'))
        self.assertEqual(model.await_count, 2)
        stages, usage = await self.snapshot()
        self.assertEqual(usage.llm_calls, 2)
        self.assertEqual(stages['analysis_part_0'].state, 'complete')
        self.assertEqual(stages['analysis_part_1'].state, 'complete')
        self.assertFalse(any(row.state == 'running' for row in stages.values()))
        async with self.sessions() as session:
            self.assertIsNone(await session.get(Summary, self.bvid))

    async def test_nonshrinking_merge_input_keeps_the_existing_bounded_failure(self):
        await self.prepare()
        oversized = analyzer._empty_analysis()
        oversized.update(summary='s' * 2000, key_points=['p' * 500] * 8,
                         risk_warnings=['r' * 500] * 8, data_citations=['d' * 500] * 8)
        for attempt in range(3):
            responses = [self.response(data=oversized), self.response(data=oversized)] if attempt == 0 else []
            result, model = await self.process(responses)
            self.assertEqual((result.reason, result.stage), ('analysis_intermediate_too_large', 'analysis'))
            self.assertEqual(model.await_count, 2 if attempt == 0 else 0)
        stages, usage = await self.snapshot()
        self.assertEqual(stages['analysis'].attempts, 3)
        self.assertFalse(any(row.state == 'running' for row in stages.values()))
        self.assertEqual(usage.llm_calls, 2)
        async with self.sessions() as session:
            self.assertEqual((await session.get(VideoWork, self.bvid)).state, 'exhausted')
        result, model = await self.process([])
        self.assertEqual((result.reason, result.stage), ('retry_exhausted', 'analysis'))
        model.assert_not_awaited()
