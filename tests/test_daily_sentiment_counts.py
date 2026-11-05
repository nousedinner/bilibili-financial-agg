"""每日汇总 sentiment_counts（多空比例）口径回归测试。

口径必须与 src/fetcher.py `_generate_digest` 一致：
窗口 [date-1 cutoff, date cutoff)（cutoff 默认 22）、fetch_status='ok'、仅启用博主。
同时验证 LLM 判断（overall_sentiment）与分数（sentiment_score）互不覆写。
"""
import copy
import os
import tempfile
import unittest
from datetime import datetime, date, time, timedelta
from pathlib import Path
from unittest.mock import patch

for key, value in {'DB_USER': 'review', 'DB_PASSWORD': 'review', 'DB_HOST': '127.0.0.1',
                   'XIAOMI_API_KEY': 'TEST_ONLY', 'SESSDATA': ''}.items():
    os.environ[key] = value

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src import main, fetcher, workflow, collection, pipeline, authorization, ownership, lifecycle, auth_routes, config
from src.models import Base, Blogger, Video, Summary, DailyDigest


class DailySentimentCounts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_config = copy.deepcopy(config._CONFIG)
        self.temp = tempfile.TemporaryDirectory(prefix='fin-daily-counts-')
        config._CONFIG = {'limits': {'request_interval_seconds': 0}, 'data': {
            'credential_path': str(Path(self.temp.name) / 'credential.json'),
            'outbox_path': str(Path(self.temp.name) / 'outbox.db')}}
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        # /api/daily 列表 SQL 用到 MySQL 专有函数，sqlite 下补等价实现；
        # 必须在第一条连接建立前注册（create_all 会建立第一条连接）。
        @event.listens_for(self.engine.sync_engine, "connect")
        def _sqlite_mysql_compat(dbapi_connection, connection_record):
            dbapi_connection.create_function("json_unquote", 1, lambda v: v)
            dbapi_connection.create_function("left", 2, lambda s, n: (s or "")[:int(n)])

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch.object(main, 'async_session', self.sessions),
                        patch.object(fetcher, 'async_session', self.sessions)]
        for item in self.patches:
            item.start()
        main.app.dependency_overrides[main.require_api_key] = lambda: None
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                      base_url='http://fixture.invalid')

    async def asyncTearDown(self):
        await self.http.aclose()
        main.app.dependency_overrides.clear()
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        fetcher.outbox_close()
        self.temp.cleanup()
        config._CONFIG = self.previous_config

    async def test_counts_follow_window_and_enabled_filter(self):
        async with self.sessions() as s:
            s.add(Blogger(mid=1, name='On', enabled=True))
            s.add(Blogger(mid=2, name='Off', enabled=False))
            rows = [
                ('BVc0', 1, datetime(2026, 9, 10, 10, 0), 'bullish', 0.5),
                ('BVc1', 1, datetime(2026, 9, 10, 11, 0), 'bearish', -0.6),
                ('BVc2', 1, datetime(2026, 9, 10, 12, 0), 'neutral', 0.0),
                ('BVc3', 2, datetime(2026, 9, 10, 13, 0), 'bullish', 0.7),  # 停用博主 → 排除
                ('BVc4', 1, datetime(2026, 9, 11, 10, 0), 'bullish', 0.9),  # 次日 → 排除
                ('BVc5', 1, datetime(2026, 9, 9, 21, 0), 'bullish', 0.9),   # 前一日22点前 → 排除
            ]
            for bvid, mid, at, sentiment, score in rows:
                s.add(Video(bvid=bvid, mid=mid, title=bvid, publish_time=at,
                            fetch_status='ok', analysis_status='completed'))
                s.add(Summary(bvid=bvid, summary='x', key_points=[],
                              sentiment=sentiment, sentiment_score=score))
            s.add(DailyDigest(digest_date=date(2026, 9, 10),
                              content={'summary': 's', 'overall_sentiment': 'bullish',
                                       'sentiment_score': -0.03}))
            await s.commit()

        expected = {'bullish': 1, 'bearish': 1, 'neutral': 1, 'total': 3}
        data = (await self.http.get('/api/daily/2026-09-10')).json()['data']
        self.assertEqual(data['sentiment_counts'], expected)
        # 三个字段并列、互不覆写
        self.assertEqual(data['overall_sentiment'], 'bullish')
        self.assertEqual(data['sentiment_score'], -0.03)

        listing = (await self.http.get('/api/daily')).json()['data']
        self.assertEqual(listing['details']['2026-09-10']['sentiment_counts'], expected)

    async def test_counts_absent_without_videos(self):
        async with self.sessions() as s:
            s.add(DailyDigest(digest_date=date(2026, 9, 11),
                              content={'summary': 's', 'overall_sentiment': 'neutral'}))
            await s.commit()
        data = (await self.http.get('/api/daily/2026-09-11')).json()['data']
        self.assertNotIn('sentiment_counts', data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
