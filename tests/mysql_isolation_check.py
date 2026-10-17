"""Explicit opt-in MySQL integration. Refuses the production database name."""
import asyncio
import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import text, select, func

from src.config import get_config
from src.db import engine, async_session
from src import migrations, fetcher, analyzer, workflow
from src.lifecycle import now, owner_context, OwnershipLost
from src.models import Base, Blogger, Video, VideoWork, TranscriptCheckpoint, Transcript, Summary
from src.ownership import OwnerGuard


async def main():
    database = engine.url.database or ''
    if not database.startswith('fin_agg_audit_') or database == 'fin_agg':
        raise RuntimeError('Refusing integration test outside the isolated audit schema')
    if os.environ.get('XIAOMI_API_KEY') != 'TEST_ONLY' or os.environ.get('SESSDATA'):
        raise RuntimeError('Real upstream credentials must not be available to integration tests')
    async with engine.begin() as connection:
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
    async with async_session() as session:
        session.add(Blogger(mid=1,name='integration',enabled=True))
        session.add_all([Video(bvid=f'BVlegacy{i}',mid=1,publish_time=now()-timedelta(days=30),
            fetch_status='failed',analysis_status='pending',retry_count=3) for i in range(100)])
        await session.commit()
    assert await workflow.due_work(async_session)==[]
    async with async_session() as session:
        assert (await session.execute(select(func.count()).select_from(VideoWork))).scalar()==0

    first=OwnerGuard(engine,async_session,heartbeat_seconds=3600,lock_name=database)
    second=OwnerGuard(engine,async_session,heartbeat_seconds=3600,lock_name=database)
    await first.start()
    old_ticket=first.ticket()
    async with first.io_lock:
        connection_id=(await first.connection.execute(text('SELECT CONNECTION_ID()'))).scalar()
        # Shorten only the isolated test connection's timeout. Never KILL a
        # production connection or change a global MySQL setting.
        await first.connection.execute(text('SET SESSION wait_timeout=2'))
    await asyncio.sleep(3)
    await second.start()
    rejected=False
    try:
        async with async_session() as session:
            await old_ticket.fence(session)
    except OwnershipLost:
        rejected=True
    assert rejected
    token=owner_context.set(second.ticket())
    try:
        from zoneinfo import ZoneInfo
        published=now()-timedelta(hours=1)
        payload={'bvid':'BVmysql','title':'integration','created':published.replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(),'length':7,'aid':123}
        batch=await workflow.create_batch(async_session,origin='incremental',since=now()-timedelta(days=7),until=now(),mid=1)
        assert await workflow.admit(async_session,batch,1,payload)
        await workflow.freeze_batch(async_session,batch)
        client=type('Client',(),{})()
        client.credential_version=1
        client.get_video_info=AsyncMock(return_value={'cid':77,'duration':7})
        client.get_comments=AsyncMock(return_value={'total':0,'replies':[]})
        client.get_danmaku=AsyncMock(return_value={'total':0,'items':[]})
        good=analyzer._empty_analysis();good['summary']='mysql integration verified'
        transcribe=AsyncMock(return_value={'source':'asr','text':'complete checkpoint','segments':1})
        with patch.object(fetcher,'fetch_transcript',transcribe),patch.object(fetcher,'analyze_video',AsyncMock(side_effect=[analyzer._empty_analysis(True),good])):
            try:
                await fetcher._process_video(client,1,payload)
            except ValueError:
                pass
            async with async_session() as session:
                assert (await session.execute(select(func.count()).select_from(TranscriptCheckpoint))).scalar()==1
                assert await session.get(Transcript,'BVmysql') is None
            await fetcher._process_video(client,1,payload,True)
        assert transcribe.await_count==1
        async with async_session() as session:
            assert (await session.get(Summary,'BVmysql')).summary=='mysql integration verified'
            assert (await session.get(Video,'BVlegacy0')).retry_count==3
            assert (await session.execute(select(func.count()).select_from(VideoWork))).scalar()==1
        await second.check()
        print(json.dumps({'database':database,'migration_idempotent':True,'legacy_rows_enqueued':0,
            'real_mysql_idle_timeout_released_lock':True,'stale_generation_rejected':rejected,
            'new_owner_generation':second.generation,'checkpoint_reused_after_llm_failure':True,
            'paid_upstream_calls':0},ensure_ascii=True))
    finally:
        owner_context.reset(token)
        await first.close()
        await second.close()
        await engine.dispose()


if __name__=='__main__':
    asyncio.run(main())
