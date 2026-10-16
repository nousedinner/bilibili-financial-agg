"""Failure injection and history-admission tests. No paid or real upstream calls."""
import asyncio
from datetime import datetime, timedelta
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, func, text
from tests import test_acceptance as baseline
from src import main, fetcher, analyzer, bilibili, transcript, workflow, collection, authorization, config, migrations
from src.credentials import credential_store
from src.lifecycle import WorkflowError, OwnershipLost, work_context, owner_context, automatic_floor
from src.models import *
from src.ownership import OwnerGuard


def good_analysis():
    result = analyzer._empty_analysis()
    result['summary'] = 'Verified whole video'
    return result


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = baseline.Acceptance.asyncSetUp
    asyncTearDown = baseline.Acceptance.asyncTearDown
    process = baseline.Acceptance.process

    def payload(self, bvid='BVnew', age=timedelta(hours=1)):
        from zoneinfo import ZoneInfo
        return {'bvid': bvid, 'title': 'fixture', 'created': (self.at-age).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp(), 'length': 7, 'aid': 123}

    def client(self):
        return SimpleNamespace(credential_version=1,
            get_video_info=AsyncMock(return_value={'cid':77,'duration':7,'pages':[{'cid':77,'duration':7}]}),
            get_comments=AsyncMock(return_value={'replies':[],'total':0}),
            get_danmaku=AsyncMock(return_value={'items':[],'total':0}),
            get_video_aid=AsyncMock(return_value=123))

    async def admit(self, bvid='BVnew', origin='incremental'):
        async with self.sessions() as session:
            if await session.get(Blogger,1) is None:
                session.add(Blogger(mid=1,name='fixture',enabled=True))
                await session.commit()
        batch = await workflow.create_batch(self.sessions, origin=origin,
            since=automatic_floor(self.at) if origin=='incremental' else workflow.HARD_SINCE,
            until=self.at, max_videos=5,max_pages=5,mid=1)
        await workflow.admit(self.sessions,batch,1,self.payload(bvid),allow_legacy=origin=='manual_backfill')
        await workflow.freeze_batch(self.sessions,batch)
        return batch

    async def count(self, model):
        async with self.sessions() as session:
            return (await session.execute(select(func.count()).select_from(model))).scalar()

    async def test_upgrade_restart_model_change_does_not_enqueue_legacy(self):
        async with self.sessions() as session:
            session.add(Blogger(mid=1,name='fixture',enabled=True))
            session.add_all([Video(bvid=f'BVlegacy{i}',mid=1,publish_time=self.at-timedelta(days=20),
                fetch_status='failed',analysis_status='pending',retry_count=8) for i in range(1000)])
            await session.commit()
        async with self.engine.begin() as connection:
            await connection.run_sync(migrations.upgrade)
            await connection.run_sync(migrations.upgrade)
        await main._recover_interrupted()
        config._CONFIG['llm']={'model':'changed-model'}
        self.assertEqual(await workflow.due_work(self.sessions),[])
        self.assertEqual(await self.count(VideoWork),0)
        self.assertEqual(await self.count(WorkStage),0)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Video,'BVlegacy0')).retry_count,8)

    async def test_processing_entry_rejects_unadmitted_legacy_before_network(self):
        client=self.client()
        with self.assertRaisesRegex(WorkflowError,'not_admitted'):
            await fetcher._process_video(client,1,self.payload('BVlegacy'),True)
        client.get_video_info.assert_not_awaited()
        self.assertEqual(await self.count(Video),0)

    async def test_repeated_subtitle_failure_exhausts_only_its_own_stage(self):
        await self.admit()
        client = self.client()
        client.get_cc_subtitle = AsyncMock(side_effect=bilibili.BiliAPIError(-352, 'fixture risk block'))
        client.get_subtitle_url = AsyncMock(return_value=None)
        with patch.object(transcript, '_asr_transcribe', AsyncMock()) as asr, \
                patch.object(fetcher, 'analyze_video', AsyncMock()) as llm:
            for _ in range(3):
                with self.assertRaises(bilibili.BiliAPIError):
                    await fetcher._process_video(client, 1, self.payload())
            with self.assertRaisesRegex(WorkflowError, 'retry_exhausted'):
                await fetcher._process_video(client, 1, self.payload())
        client.get_cc_subtitle.assert_awaited()
        self.assertEqual(client.get_cc_subtitle.await_count, 3)
        asr.assert_not_awaited()
        llm.assert_not_awaited()
        async with self.sessions() as db:
            self.assertEqual((await db.get(WorkStage, ('BVnew', 'subtitle'))).attempts, 3)
            self.assertEqual((await db.get(WorkStage, ('BVnew', 'metadata'))).attempts, 1)
            self.assertEqual((await db.get(WorkStage, ('BVnew', 'asr'))).attempts, 0)
            self.assertEqual((await db.get(VideoWork, 'BVnew')).state, 'exhausted')
            self.assertIsNone(await db.get(Summary, 'BVnew'))

    async def test_metadata_risk_keeps_its_full_cooldown_without_starting_transcription(self):
        await self.admit()
        async with bilibili.BiliClient('TEST_SESSION') as client:
            with patch.object(bilibili,'_run_curl',AsyncMock(side_effect=bilibili.BiliAPIError(-352,'fixture risk'))),patch.object(fetcher,'fetch_transcript',AsyncMock()) as transcribe:
                with self.assertRaises(bilibili.BiliAPIError) as raised:
                    await fetcher._process_video(client,1,self.payload())
                self.assertEqual(raised.exception.code,-352)
                transcribe.assert_not_awaited()
        async with self.sessions() as db:
            work=await db.get(VideoWork,'BVnew')
            self.assertEqual(work.reason,'risk_blocked')
            self.assertEqual(work.next_attempt_at,self.at+timedelta(seconds=900))
            self.assertEqual((await db.get(WorkStage,('BVnew','metadata'))).attempts,1)

    async def test_aid_lookup_preserves_transport_and_json_risk_errors(self):
        async with bilibili.BiliClient('TEST_SESSION') as client:
            for code in (412,-352):
                with patch.object(bilibili,'_run_curl',AsyncMock(side_effect=bilibili.BiliAPIError(code,'fixture'))):
                    with self.assertRaises(bilibili.BiliAPIError) as raised:
                        await client.get_video_aid('BVnew')
                    self.assertEqual(raised.exception.code,code)
            with patch.object(bilibili,'_run_curl',AsyncMock(return_value=(json.dumps({'code':-352,'message':'fixture risk'}).encode(),200))):
                with self.assertRaises(bilibili.BiliAPIError) as raised:
                    await client.get_video_aid('BVnew')
                self.assertEqual(raised.exception.reason,'risk_blocked')

    async def test_hard_and_recent_boundaries_reject_before_queue_creation(self):
        batch=await workflow.create_batch(self.sessions,origin='incremental',since=automatic_floor(self.at),until=self.at)
        for age in (timedelta(days=8),timedelta(days=200)):
            with self.assertRaises(WorkflowError):
                await workflow.admit(self.sessions,batch,1,self.payload(age=age))
        with self.assertRaises(WorkflowError):
            await fetcher._process_video(self.client(),1,{'bvid':'BVancient','created':0})
        self.assertEqual(await self.count(VideoWork),0)

    async def test_manual_scope_freezes_membership_across_restart(self):
        batch=await self.admit(origin='manual_backfill')
        with self.assertRaisesRegex(WorkflowError,'scope_frozen'):
            await workflow.admit(self.sessions,batch,1,self.payload('BVother'),allow_legacy=True)
        await main._recover_interrupted()
        async with self.sessions() as session:
            self.assertEqual((await session.get(WorkBatch,batch)).selected_bvids,['BVnew'])
        self.assertEqual(await self.count(VideoWork),1)

    async def test_missing_watermark_only_establishes_current_start(self):
        client=SimpleNamespace(get_video_list=AsyncMock(return_value={'list':{'vlist':[self.payload('BVold',timedelta(days=80))]}}))
        process=AsyncMock()
        await collection.scan_blogger(self.sessions,client,1,'fixture',process,budget={'pages':5,'new_videos':20,'retries':0})
        process.assert_not_awaited()
        self.assertEqual(await self.count(VideoWork),0)
        async with self.sessions() as session:
            scan=(await session.execute(select(DiscoveryScan))).scalar_one()
            self.assertEqual(scan.state,'initialized')

    async def test_partial_discovery_retains_marker_and_durable_discoveries(self):
        async with self.sessions() as session:
            session.add(Blogger(mid=1,name='fixture',enabled=True))
            session.add(FetchWatermark(mid=1,last_bvid='BVanchor',last_publish_time=self.at-timedelta(days=1)))
            await session.commit()
        client=SimpleNamespace(get_video_list=AsyncMock(side_effect=[
            {'list':{'vlist':[self.payload('BVnew')]}},bilibili.BiliAPIError(412,'HTTP 412')]))
        await collection.scan_blogger(self.sessions,client,1,'fixture',AsyncMock(),budget={'pages':3,'new_videos':3,'retries':0})
        async with self.sessions() as session:
            self.assertEqual((await session.get(FetchWatermark,1)).last_bvid,'BVanchor')
            self.assertIsNotNone(await session.get(VideoWork,'BVnew'))
            self.assertEqual((await session.execute(select(DiscoveryScan))).scalar_one().state,'partial')

    async def test_checkpoint_survives_model_failure_and_skips_second_transcription(self):
        await self.admit()
        transcribe=AsyncMock(return_value={'source':'asr','text':'complete original text','segments':3})
        analyze=AsyncMock(side_effect=[analyzer._empty_analysis(True),good_analysis()])
        client=self.client()
        with patch.object(fetcher,'fetch_transcript',transcribe),patch.object(fetcher,'analyze_video',analyze):
            with self.assertRaises(WorkflowError):
                await fetcher._process_video(client,1,self.payload())
            self.assertEqual(await self.count(TranscriptCheckpoint),1)
            self.assertEqual(await self.count(Transcript),0)
            self.at += timedelta(minutes=10)
            await fetcher._process_video(client,1,self.payload(),True)
        self.assertEqual(transcribe.await_count,1)
        self.assertEqual(analyze.await_count,2)
        self.assertEqual(client.get_comments.await_count,1)  # genuine zero is cached too
        self.assertEqual(await self.count(Summary),1)

    async def test_danmaku_failure_keeps_body_and_retry_does_not_refetch_comments(self):
        await self.admit()
        client=self.client()
        client.get_danmaku.side_effect=bilibili.BiliAPIError(412,'HTTP 412')
        transcribe=AsyncMock(return_value={'source':'asr','text':'complete original text','segments':1})
        analyze=AsyncMock(return_value=good_analysis())
        updated={key:good_analysis()[key] for key in ('comment_sentiment','danmaku_sentiment','comment_keywords','danmaku_keywords')}
        with patch.object(fetcher,'fetch_transcript',transcribe),patch.object(fetcher,'analyze_video',analyze),patch.object(analyzer,'analyze_interactions',AsyncMock(return_value=updated)) as social:
            result=await fetcher._process_video(client,1,self.payload())
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['danmaku_status'],'unavailable')
            self.at += timedelta(minutes=10)
            client.get_danmaku.side_effect=None
            await fetcher._process_video(client,1,self.payload(),True)
        self.assertEqual(transcribe.await_count,1)
        self.assertEqual(client.get_comments.await_count,1)
        self.assertEqual(analyze.await_count,1)
        self.assertEqual(social.await_count,1)

    async def test_authentication_metadata_is_not_no_track(self):
        async with bilibili.BiliClient('TEST_ONLY') as client:
            with patch.object(client,'_get',AsyncMock(return_value={'code':0,'data':{'need_login_subtitle':True,'subtitle':{'subtitles':[]}}})):
                with self.assertRaises(bilibili.BiliAPIError) as error:
                    await client.get_subtitle_url('BVnew',77)
        self.assertEqual(error.exception.reason,'auth_required')

    async def test_network_or_http_block_is_unknown_auth(self):
        async with bilibili.BiliClient('TEST_ONLY') as client:
            for exception in (TimeoutError(),bilibili.BiliAPIError(412,'blocked'),bilibili.BiliAPIError(-352,'risk')):
                with patch.object(bilibili,'_run_curl',AsyncMock(side_effect=exception)):
                    result=await client.validate_sessdata()
                self.assertEqual(result['state'],'unknown')
                self.assertIsNone(result['valid'])

    async def test_initial_invalid_alert_is_deduplicated_and_reminded_after_day(self):
        for _ in range(4):
            await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        self.assertEqual(await self.count(AuthIncident),1)
        self.assertEqual(await self.count(AlertDelivery),1)
        self.at += timedelta(hours=25)
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        self.assertEqual(await self.count(AlertDelivery),2)

    async def test_unknown_never_opens_or_resolves_expiry_incident(self):
        await authorization.record_auth_check(self.sessions,1,'unknown','http_blocked')
        self.assertEqual(await self.count(AuthIncident),0)
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        await authorization.record_auth_check(self.sessions,1,'unknown','transient')
        async with self.sessions() as session:
            state=await session.get(AuthState,1)
            self.assertEqual(state.last_confirmed,'invalid')
            self.assertIsNone((await session.get(AuthIncident,state.incident_id)).resolved_at)
        await authorization.record_auth_check(self.sessions,1,'valid')
        async with self.sessions() as session:
            self.assertIsNotNone((await session.get(AuthState,1)).recovered_at)

    async def test_old_probe_cannot_overwrite_new_credential_health(self):
        await credential_store().replace({'sessdata':'TEST_NEW','dedeuserid':'1'},expected_version=1)
        await authorization.record_auth_check(self.sessions,2,'valid')
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        status=await authorization.public_auth_status(self.sessions)
        self.assertEqual(status['state'],'valid')
        self.assertEqual(await self.count(AuthIncident),0)

    async def test_concurrent_credential_replacement_is_compare_and_swap(self):
        first,second=credential_store(),credential_store()
        self.assertIs(first,second)
        results=await asyncio.gather(first.replace({'sessdata':'TEST_ONE'},expected_version=1),
            second.replace({'sessdata':'TEST_TWO'},expected_version=1),return_exceptions=True)
        self.assertEqual(sum(isinstance(result,ValueError) for result in results),1)
        self.assertEqual(credential_store().current().version,2)

    async def test_unknown_reminds_unresolved_incident_without_claiming_expiry(self):
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        self.at += timedelta(hours=25)
        await authorization.record_auth_check(self.sessions,1,'unknown','http_blocked')
        self.assertEqual(await self.count(AlertDelivery),2)
        status=await authorization.public_auth_status(self.sessions)
        self.assertEqual(status['state'],'unknown')
        self.assertEqual(status['last_confirmed'],'invalid')

    async def test_same_timestamp_anchor_does_not_hide_next_page_candidates(self):
        from zoneinfo import ZoneInfo
        async with self.sessions() as session:
            session.add(Blogger(mid=1,name='fixture',enabled=True))
            session.add(FetchWatermark(mid=1,last_bvid='BVanchor',last_publish_time=self.at-timedelta(hours=1)))
            await session.commit()
        client=SimpleNamespace(get_video_list=AsyncMock(side_effect=[
            {'list':{'vlist':[self.payload('BVtie1'),self.payload('BVanchor')]}},
            {'list':{'vlist':[self.payload('BVtie2'),self.payload('BVoldearlier',timedelta(hours=2))]}}]))
        await collection.scan_blogger(self.sessions,client,1,'fixture',AsyncMock(return_value={'status':'complete'}),
            budget={'pages':5,'new_videos':5,'retries':0})
        async with self.sessions() as session:
            self.assertIsNotNone(await session.get(VideoWork,'BVtie1'))
            self.assertIsNotNone(await session.get(VideoWork,'BVtie2'))
            self.assertIsNone(await session.get(VideoWork,'BVoldearlier'))

    async def test_initialized_start_does_not_drift_back_on_later_scans(self):
        first=self.payload('BVanchor',timedelta(days=2))
        client=SimpleNamespace(get_video_list=AsyncMock(return_value={'list':{'vlist':[first]}}))
        await collection.scan_blogger(self.sessions,client,1,'fixture',AsyncMock(),budget={'pages':2,'new_videos':5,'retries':0})
        self.at += timedelta(hours=1)
        client.get_video_list=AsyncMock(return_value={'list':{'vlist':[self.payload('BVlateold',timedelta(days=1)),first]}})
        await collection.scan_blogger(self.sessions,client,1,'fixture',AsyncMock(),budget={'pages':2,'new_videos':5,'retries':0})
        self.assertEqual(await self.count(VideoWork),0)

    async def test_reauthorization_only_wakes_explicit_in_scope_auth_work(self):
        await self.admit('BVblocked')
        await self.admit('BVexpired')
        async with self.sessions() as session:
            for bvid in ('BVblocked','BVexpired'):
                work=await session.get(VideoWork,bvid)
                work.state,work.credential_version='blocked_auth',1
            (await session.get(VideoWork,'BVexpired')).expires_at=self.at-timedelta(seconds=1)
            session.add(Video(bvid='BVlegacy',mid=1,publish_time=self.at,fetch_status='failed',error_type='transcript',retry_count=3))
            await session.commit()
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        await credential_store().replace({'sessdata':'TEST_NEW','dedeuserid':'1'},expected_version=1)
        # An indeterminate first check of v2 must not lose the association to v1.
        await authorization.record_auth_check(self.sessions,2,'unknown','transient')
        await authorization.record_auth_check(self.sessions,2,'valid')
        jobs=await workflow.due_work(self.sessions)
        self.assertEqual([job.bvid for job in jobs],['BVblocked'])
        self.assertEqual(jobs[0].last_trigger,'auth_recovery')
        self.assertEqual(await self.count(VideoWork),2)

    async def test_no_push_configuration_keeps_outbox_and_disallows_wait_policy(self):
        config._CONFIG['asr']={'auth_failure_policy':'wait'}
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        with patch.object(authorization,'push_sender',return_value=SimpleNamespace(configured=lambda:False)):
            await authorization.deliver_alerts(self.sessions)
            self.assertFalse(await authorization.auth_wait_enabled(self.sessions))
        async with self.sessions() as session:
            delivery=(await session.execute(select(AlertDelivery))).scalar_one()
            self.assertEqual(delivery.state,'waiting_configuration')
            self.assertIsNone(delivery.acknowledged_at)

    async def test_provider_acceptance_is_not_device_confirmation(self):
        async with self.sessions() as session:
            session.add(AppDevice(id=1,token='TEST_DEVICE_TOKEN_1234567890',notification_enabled=True))
            await session.commit()
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        sender=SimpleNamespace(configured=lambda:True,send=AsyncMock())
        with patch.object(authorization,'push_sender',return_value=sender):
            await authorization.deliver_alerts(self.sessions)
        sender.send.assert_awaited_once()
        async with self.sessions() as session:
            delivery=(await session.execute(select(AlertDelivery))).scalar_one()
            self.assertEqual(delivery.state,'submitted')
            self.assertIsNone(delivery.acknowledged_at)

    async def test_missing_push_configuration_does_not_fake_success(self):
        with patch.object(authorization,'push_sender',return_value=SimpleNamespace(configured=lambda:False)):
            response=await self.http.post('/api/notifications/test')
        self.assertEqual(response.status_code,503)

    async def test_unregistered_push_disables_wait_without_enqueuing_history(self):
        await self.admit('BVblocked')
        config._CONFIG['asr']={'auth_failure_policy':'wait'}
        async with self.sessions() as session:
            (await session.get(VideoWork,'BVblocked')).state='blocked_auth'
            session.add(AppDevice(id=1,token='TEST_DEVICE_TOKEN_1234567890',notification_enabled=True,verified_at=self.at))
            session.add(Video(bvid='BVlegacy',mid=1,fetch_status='failed',publish_time=self.at,retry_count=3))
            await session.commit()
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        sender=SimpleNamespace(configured=lambda:True,send=AsyncMock(side_effect=authorization.PushFailure('device_unregistered')))
        with patch.object(authorization,'push_sender',return_value=sender):
            self.assertEqual(await workflow.due_work(self.sessions),[])
            await authorization.deliver_alerts(self.sessions)
            jobs=await workflow.due_work(self.sessions)
            self.assertEqual([job.bvid for job in jobs],['BVblocked'])
        self.assertEqual(await self.count(VideoWork),1)

    async def test_authorization_endpoints_require_a_valid_enabled_connection_key(self):
        async with self.sessions() as db:
            db.add_all([ApiUser(username='owner', api_key='TEST_CONNECTION_OWNER_KEY', enabled=True),
                        ApiUser(username='disabled', api_key='TEST_DISABLED_KEY', enabled=False)])
            await db.commit()
        main.app.dependency_overrides.pop(main.require_api_key)
        paths=[('/api/bili/auth/check',None),('/api/bili/auth/sessions',None),
               ('/api/bili/auth/sessions/absent/poll',{'nonce':'TEST_NONCE_LONG_ENOUGH_1234'}),
               ('/api/work/retry',{'bvids':['BVabsent']})]
        with patch.object(bilibili.BiliClient,'_get',AsyncMock()) as upstream:
            for headers in ({},{'X-API-Key':'wrong'},{'X-API-Key':'TEST_DISABLED_KEY'},
                            {'X-Management-Key':'retired-key'}):
                for path,body in paths:
                    response=await self.http.post(path,headers=headers,json=body)
                    self.assertEqual(response.status_code,401,(path,response.status_code))
            upstream.assert_not_awaited()
        self.assertEqual(await self.count(LoginSession),0)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_connection_key_alone_can_create_login_without_a_second_secret(self):
        async with self.sessions() as db:
            db.add(ApiUser(username='owner',api_key='TEST_CONNECTION_OWNER_KEY',enabled=True))
            await db.commit()
        main.app.dependency_overrides.pop(main.require_api_key)
        upstream={'code':0,'data':{'url':'https://account.bilibili.com/scan?qrcode_key=TEST_QR','qrcode_key':'TEST_QR'}}
        with patch.object(bilibili.BiliClient,'_get',AsyncMock(return_value=upstream)):
            response=await self.http.post('/api/bili/auth/sessions',headers={'X-API-Key':'TEST_CONNECTION_OWNER_KEY'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(await self.count(LoginSession),1)
        self.assertEqual(credential_store().current().version,1)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_relogin_keeps_existing_account_when_environment_uid_is_missing(self):
        from src import auth_routes
        from urllib.parse import urlencode
        from uuid import uuid4
        import hashlib
        await credential_store().replace({'sessdata':'TEST_OLD_SESSION','bili_jct':'TEST_CSRF','dedeuserid':'1'},expected_version=1)
        session_id=str(uuid4());nonce='TEST_NONCE_ACCOUNT_PIN_12345'
        async with self.sessions() as db:
            db.add(LoginSession(id=session_id,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                qrcode_key='TEST_QR',credential_version=2,state='pending',expires_at=self.at+timedelta(minutes=2)))
            await db.commit()
        callback='https://passport.bilibili.com/callback?'+urlencode({'SESSDATA':'TEST_OTHER_SESSION','bili_jct':'TEST_CSRF','DedeUserID':'2'})
        upstream=[{'code':0,'data':{'code':0,'url':callback}},{'code':0,'data':{'mid':2,'isLogin':True}}]
        with patch.object(auth_routes,'now',lambda:self.at),patch.dict('os.environ',{'BILI_UID':''}),patch.object(bilibili.BiliClient,'_get',AsyncMock(side_effect=upstream)),patch.object(bilibili.BiliClient,'validate_sessdata',AsyncMock(return_value={'state':'valid'})):
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(credential_store().current().version,2)
        self.assertEqual(credential_store().current().dedeuserid,'1')
        self.assertEqual(await self.count(VideoWork),0)

    async def test_qr_candidate_is_verified_before_replacing_and_never_returned(self):
        from urllib.parse import urlencode
        secret='TEST_SESSION_NEVER_RETURN'
        upstream=[{'code':0,'data':{'url':'https://passport.bilibili.com/scan?qrcode_key=TEST_QR','qrcode_key':'TEST_QR'}},
            {'code':0,'data':{'code':0,'url':'https://passport.bilibili.com/callback?'+urlencode({'SESSDATA':secret,'bili_jct':'TEST_CSRF','DedeUserID':'1'})}},
            {'code':0,'data':{'mid':1,'isLogin':True}}]
        headers={}
        with patch.dict('os.environ',{'BILI_UID':'1'}),patch.object(bilibili.BiliClient,'_get',AsyncMock(side_effect=upstream)),patch.object(bilibili.BiliClient,'validate_sessdata',AsyncMock(return_value={'state':'valid'})):
            created=await self.http.post('/api/bili/auth/sessions',headers=headers)
            self.assertEqual(created.status_code,200)
            session=created.json()['data']
            completed=await self.http.post('/api/bili/auth/sessions/'+session['id']+'/poll',headers=headers,json={'nonce':session['nonce']})
        self.assertEqual(completed.status_code,200,completed.text)
        self.assertEqual(credential_store().current().version,2)
        self.assertNotIn(secret,completed.text)
        self.assertEqual(await self.count(VideoWork),0)

    async def seed_login_after_file_swap(self, *, matching):
        from uuid import uuid4
        from src import login_checkpoint
        session_id=str(uuid4());nonce='TEST_NONCE_AFTER_FILE_SWAP_12345'
        expires=self.at+timedelta(minutes=2)
        candidate={'sessdata':'TEST_SWAPPED_SESSION','bili_jct':'TEST_CSRF','dedeuserid':'1'}
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        await credential_store().replace(candidate,expected_version=1)
        async with self.sessions() as db:
            db.add(LoginSession(id=session_id,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                qrcode_key='TEST_OLD_QR',credential_version=1,state='pending',expires_at=expires))
            await db.commit()
        saved=candidate if matching else {**candidate,'sessdata':'TEST_OTHER_SESSION'}
        login_checkpoint.save(session_id,1,expires,{'candidate':saved})
        return session_id,nonce

    async def test_committed_credential_reconciles_pending_login_after_database_failure(self):
        from src import auth_routes,login_checkpoint
        session_id,nonce=await self.seed_login_after_file_swap(matching=True)
        with patch.object(auth_routes,'now',lambda:self.at),patch.object(bilibili.BiliClient,'_get') as upstream:
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,200,response.text)
        upstream.assert_not_called()
        self.assertNotIn('TEST_SWAPPED_SESSION',response.text)
        self.assertEqual(response.json()['data']['credential_version'],2)
        async with self.sessions() as db:
            row=await db.get(LoginSession,session_id)
            self.assertEqual(row.state,'complete')
            self.assertEqual(row.qrcode_key,'')
            self.assertEqual((await db.get(AuthState,1)).state,'valid')
        self.assertFalse(login_checkpoint._path(session_id).exists())
        self.assertEqual(await self.count(VideoWork),0)

    async def test_other_session_candidate_cannot_claim_committed_credential(self):
        from src import auth_routes
        session_id,nonce=await self.seed_login_after_file_swap(matching=False)
        with patch.object(auth_routes,'now',lambda:self.at),patch.object(bilibili.BiliClient,'_get') as upstream:
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,409,response.text)
        upstream.assert_not_called()
        self.assertEqual(credential_store().current().version,2)
        async with self.sessions() as db:
            self.assertEqual((await db.get(LoginSession,session_id)).state,'pending')
            self.assertEqual((await db.get(AuthState,1)).state,'invalid')
        self.assertEqual(await self.count(VideoWork),0)

    async def test_completed_login_cannot_overwrite_new_invalid_evidence(self):
        from src import auth_routes
        session_id,nonce=await self.seed_login_after_file_swap(matching=True)
        async with self.sessions() as db:
            row=await db.get(LoginSession,session_id);row.state='complete';row.qrcode_key=''
            await db.commit()
        await authorization.record_auth_check(self.sessions,2,'invalid','auth_required')
        with patch.object(auth_routes,'now',lambda:self.at):
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,200,response.text)
        async with self.sessions() as db:
            self.assertEqual((await db.get(AuthState,1)).state,'invalid')
            self.assertEqual((await db.execute(select(func.count()).select_from(AlertDelivery).where(AlertDelivery.kind=='recovered'))).scalar(),0)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_expired_completed_login_does_not_reconcile_old_authorization_evidence(self):
        from src import auth_routes
        session_id,nonce=await self.seed_login_after_file_swap(matching=True)
        async with self.sessions() as db:
            row=await db.get(LoginSession,session_id);row.state='complete';row.qrcode_key=''
            row.expires_at=self.at-timedelta(minutes=1)
            await db.commit()
        with patch.object(auth_routes,'now',lambda:self.at):
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,200,response.text)
        async with self.sessions() as db:
            row=await db.get(AuthState,1)
            self.assertEqual((row.credential_version,row.state),(1,'invalid'))
        self.assertEqual(credential_store().current().version,2)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_file_swap_reconciliation_preserves_new_invalid_evidence(self):
        from src import auth_routes
        session_id,nonce=await self.seed_login_after_file_swap(matching=True)
        await authorization.record_auth_check(self.sessions,2,'invalid','auth_required')
        with patch.object(auth_routes,'now',lambda:self.at):
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',json={'nonce':nonce})
        self.assertEqual(response.status_code,200,response.text)
        async with self.sessions() as db:
            self.assertEqual((await db.get(LoginSession,session_id)).state,'complete')
            self.assertEqual((await db.get(AuthState,1)).state,'invalid')
            self.assertEqual((await db.execute(select(func.count()).select_from(AlertDelivery).where(AlertDelivery.kind=='recovered'))).scalar(),0)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_qr_generation_accepts_both_observed_official_login_hosts(self):
        headers={}
        for host in ['passport.bilibili.com','account.bilibili.com']:
            url='https://'+host+'/h5-app/passport/login/scan?qrcode_key='+'a'*32
            upstream={'code':0,'data':{'url':url,'qrcode_key':'a'*32}}
            with self.subTest(host=host),patch.object(bilibili.BiliClient,'_get',AsyncMock(return_value=upstream)):
                result=await self.http.post('/api/bili/auth/sessions',headers=headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['data']['qr_url'],url)
        self.assertEqual(await self.count(LoginSession),2)
        self.assertEqual(credential_store().current().version,1)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_qr_generation_rejects_untrusted_urls_without_creating_sessions(self):
        headers={}
        urls=['https://account.bilibili.com.evil.invalid/scan','http://account.bilibili.com/scan',
            'https://user@account.bilibili.com/scan','https://account.bilibili.com:8443/scan',
            'https://passport.bilibili.com/scan#untrusted',123]
        for url in urls:
            upstream={'code':0,'data':{'url':url,'qrcode_key':'a'*32}}
            with self.subTest(url=url),patch.object(bilibili.BiliClient,'_get',AsyncMock(return_value=upstream)):
                result=await self.http.post('/api/bili/auth/sessions',headers=headers)
            self.assertEqual(result.status_code,502)
        self.assertEqual(await self.count(LoginSession),0)
        self.assertEqual(credential_store().current().version,1)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_login_upstream_block_does_not_invalidate_server_credential(self):
        headers={}
        with patch.object(bilibili.BiliClient,'_get',AsyncMock(side_effect=bilibili.BiliAPIError(412,'fixture blocked'))):
            result=await self.http.post('/api/bili/auth/sessions',headers=headers)
        self.assertEqual(result.status_code,503)
        self.assertEqual(await self.count(LoginSession),0)
        self.assertEqual(await self.count(AuthState),0)
        self.assertEqual(credential_store().current().version,1)

    async def test_qr_ticket_exchange_preserves_set_cookie_values(self):
        import httpx
        async with bilibili.BiliClient() as client:
            response=SimpleNamespace(status_code=200,headers=httpx.Headers([
                ('set-cookie','SESSDATA=TEST%2CENCODED%2CSESSION; Domain=.biligame.com; Secure; HttpOnly'),
                ('set-cookie','bili_jct=TEST_CSRF; Path=/; Secure'),
                ('set-cookie','DedeUserID=1; Path=/; Secure')]))
            request=AsyncMock(return_value=response)
            with patch.object(client._transport,'get',request):
                result=await client.resolve_login_callback('https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=TEST_TICKET','TEST_REFRESH')
            self.assertEqual(result['sessdata'],'TEST%2CENCODED%2CSESSION')
            self.assertEqual(result['dedeuserid'],'1')
            self.assertEqual(request.await_count,1)
            self.assertFalse(request.await_args.kwargs['allow_redirects'])

    async def test_qr_ticket_rejects_external_redirects_and_credentials_in_url(self):
        async with bilibili.BiliClient() as client:
            request=AsyncMock(return_value=SimpleNamespace(status_code=302,headers={'location':'https://untrusted.invalid/callback'}))
            with patch.object(client._transport,'get',request):
                with self.assertRaises(bilibili.BiliAPIError):
                    await client.resolve_login_callback('https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=TEST')
            self.assertEqual(request.await_count,1)
            for url in ['https://untrusted.invalid/x/passport-login/web/crossDomain',
                'https://user@passport.bilibili.com/x/passport-login/web/crossDomain']:
                request.reset_mock()
                with patch.object(client._transport,'get',request),self.assertRaises(bilibili.BiliAPIError):
                    await client.resolve_login_callback(url)
                request.assert_not_awaited()

    async def test_qr_ticket_keeps_cookies_across_approved_redirect_hops(self):
        import httpx
        async with bilibili.BiliClient() as client:
            responses=[
                SimpleNamespace(status_code=302,headers=httpx.Headers([
                    ('set-cookie','SESSDATA=TEST%2CSESSION; Domain=.biligame.com; Secure'),
                    ('location','https://passport.bilibili.com/x/passport-login/web/crossDomain?ticket=TEST_NEXT')])),
                SimpleNamespace(status_code=200,headers=httpx.Headers([
                    ('set-cookie','bili_jct=TEST_CSRF; Secure'),('set-cookie','DedeUserID=1; Secure')]))]
            request=AsyncMock(side_effect=responses)
            with patch.object(client._transport,'get',request):
                candidate=await client.resolve_login_callback('https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=TEST_FIRST')
            self.assertEqual(candidate['sessdata'],'TEST%2CSESSION')
            self.assertEqual(candidate['dedeuserid'],'1')
            self.assertEqual(request.await_count,2)

    async def test_qr_one_use_response_survives_transient_exchange_and_completion_retry(self):
        from src import login_checkpoint
        headers={}
        upstream=AsyncMock(side_effect=[
            {'code':0,'data':{'url':'https://account.bilibili.com/scan?qrcode_key=TEST_QR','qrcode_key':'TEST_QR'}},
            {'code':0,'data':{'code':0,'url':'https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=TEST_TICKET','refresh_token':'TEST_REFRESH'}},
            {'code':0,'data':{'mid':1,'isLogin':True}}])
        candidate={'sessdata':'TEST%2CENCODED%2CSESSION','bili_jct':'TEST_CSRF','dedeuserid':'1','refresh_token':'TEST_REFRESH'}
        resolve=AsyncMock(side_effect=[bilibili.BiliAPIError(-1,'temporary'),candidate])
        with patch.dict('os.environ',{'BILI_UID':'1'}),patch.object(bilibili.BiliClient,'_get',upstream),patch.object(bilibili.BiliClient,'resolve_login_callback',resolve),patch.object(bilibili.BiliClient,'validate_sessdata',AsyncMock(return_value={'state':'valid'})):
            created=(await self.http.post('/api/bili/auth/sessions',headers=headers)).json()['data']
            path='/api/bili/auth/sessions/'+created['id']+'/poll'
            first=await self.http.post(path,headers=headers,json={'nonce':created['nonce']})
            self.assertEqual(first.status_code,503)
            self.assertEqual(credential_store().current().version,1)
            async with self.sessions() as db:
                row=await db.get(LoginSession,created['id'])
                row.last_polled_at-=timedelta(seconds=5)
                saved=login_checkpoint.load(row.id,row.credential_version,row.expires_at)
                self.assertIn('callback_url',saved)
                await db.commit()
            second=await self.http.post(path,headers=headers,json={'nonce':created['nonce']})
            self.assertEqual(second.status_code,200,second.text)
            repeated=await self.http.post(path,headers=headers,json={'nonce':created['nonce']})
            self.assertEqual(repeated.status_code,200,repeated.text)
        self.assertEqual(upstream.await_count,3)  # Generate, one poll, one nav; never re-poll DONE.
        self.assertEqual(credential_store().current().version,2)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_login_checkpoint_cleanup_keeps_live_and_unrelated_files(self):
        from src import login_checkpoint
        from uuid import uuid4
        old,active=str(uuid4()),str(uuid4())
        login_checkpoint.save(old,1,self.at-timedelta(minutes=1),{'callback_url':'TEST_EXPIRED'})
        login_checkpoint.save(active,1,self.at+timedelta(minutes=1),{'callback_url':'TEST_ACTIVE'})
        directory=credential_store().path.parent/'.login-checkpoints'
        unrelated=directory/'unrelated.json'
        unrelated.write_text(json.dumps({'expires_at':(self.at-timedelta(days=1)).isoformat()}))
        login_checkpoint.prune(self.at)
        self.assertFalse((directory/(old+'.json')).exists())
        self.assertTrue((directory/(active+'.json')).exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(await self.count(VideoWork),0)

    async def test_unknown_new_credential_does_not_inherit_old_invalid_evidence(self):
        await authorization.record_auth_check(self.sessions,1,'invalid','auth_required')
        await credential_store().replace({'sessdata':'TEST_REPLACEMENT','dedeuserid':'1'},expected_version=1)
        status=await authorization.public_auth_status(self.sessions)
        self.assertEqual(status['state'],'unknown')
        self.assertIsNone(status['last_confirmed'])
        self.assertIsNone(status['checked_at'])
        await authorization.record_auth_check(self.sessions,2,'unknown','network_transient')
        async with self.sessions() as db:
            row=await db.get(AuthState,1)
            self.assertEqual(row.credential_version,2)
            self.assertIsNone(row.last_confirmed)
        await authorization.record_auth_check(self.sessions,2,'invalid','auth_required')
        async with self.sessions() as db:
            events=(await db.execute(select(AlertDelivery).order_by(AlertDelivery.created_at))).scalars().all()
            current=[event for event in events if event.credential_version==2]
            self.assertEqual([event.kind for event in current],['invalid'])
            row=await db.get(AuthState,1)
            self.assertIsNone(row.recovered_at)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_completed_login_reconciles_interrupted_authorization_state_write(self):
        import hashlib
        from uuid import uuid4
        session_id=str(uuid4());nonce='TEST_NONCE_ALREADY_VALIDATED_123456'
        await credential_store().replace({'sessdata':'TEST_VALID','dedeuserid':'1'},expected_version=1)
        async with self.sessions() as db:
            db.add(LoginSession(id=session_id,nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                qrcode_key='',credential_version=1,state='complete',expires_at=self.at+timedelta(minutes=1)))
            await db.commit()
        with patch.object(bilibili.BiliClient,'_get') as upstream:
            response=await self.http.post('/api/bili/auth/sessions/'+session_id+'/poll',
                headers={},json={'nonce':nonce})
        self.assertEqual(response.status_code,200,response.text)
        upstream.assert_not_called()
        status=await authorization.public_auth_status(self.sessions)
        self.assertEqual(status['state'],'valid')
        self.assertEqual(status['credential_version'],2)
        self.assertEqual(await self.count(VideoWork),0)

    async def test_fcm_request_uses_scoped_signed_assertion_without_upstream_credentials(self):
        import httpx
        from cryptography.hazmat.primitives.asymmetric import rsa,padding
        from cryptography.hazmat.primitives import serialization,hashes
        import base64
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        account=Path(self.temp.name)/'service-account.json'
        account.write_text(json.dumps({'project_id':'test-project','client_email':'test@test-project.iam.gserviceaccount.com',
            'token_uri':'https://oauth2.googleapis.com/token','private_key':key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}))
        account.chmod(0o600)
        seen=[]
        def decode(value):return base64.urlsafe_b64decode(value+'='*(-len(value)%4))
        def handler(request):
            seen.append(str(request.url))
            if request.url.host=='oauth2.googleapis.com':
                from urllib.parse import parse_qs
                assertion=parse_qs(request.content.decode())['assertion'][0]
                header,payload,signature=assertion.split('.')
                claims=json.loads(decode(payload))
                self.assertEqual(claims['scope'],'https://www.googleapis.com/auth/firebase.messaging')
                key.public_key().verify(decode(signature),(header+'.'+payload).encode(),padding.PKCS1v15(),hashes.SHA256())
                return httpx.Response(200,json={'access_token':'TEST_ACCESS','expires_in':3600})
            self.assertEqual(request.headers['authorization'],'Bearer TEST_ACCESS')
            message=json.loads(request.content)['message']
            self.assertEqual(message['android']['priority'],'high')
            self.assertNotIn('notification',message)
            self.assertNotIn('notification',message['android'])
            self.assertEqual(message['data']['event_id'],'test:1')
            self.assertNotIn('SESSDATA',request.content.decode())
            return httpx.Response(200,json={'name':'projects/test-project/messages/test'})
        sender=authorization.FcmSender();sender.path=str(account)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(authorization.httpx,'AsyncClient',return_value=client):
            await sender.send('TEST_DEVICE_TOKEN',{'kind':'test','event_id':'test:1'})
        self.assertEqual(len(seen),2)

    async def test_stale_owner_cannot_write_or_start_another_paid_request(self):
        guard=OwnerGuard(self.engine,self.sessions,heartbeat_seconds=3600)
        await guard.start()
        old=guard.ticket()
        await guard.acquire()  # reconnect yields a new generation, even in same process
        token=owner_context.set(old)
        try:
            with self.assertRaises(OwnershipLost):
                async with self.sessions() as session:
                    await old.fence(session)
            with patch.object(transcript.httpx,'AsyncClient') as network:
                with self.assertRaises(OwnershipLost):
                    await transcript._call_asr('https://example.invalid','TEST_ONLY',b'audio','test')
                network.assert_not_called()
        finally:
            owner_context.reset(token)
            await guard.close()

    async def test_digest_result_cannot_publish_after_generation_changes(self):
        day = self.at.date()
        async with self.sessions() as db:
            db.add_all([Blogger(mid=1, name='fixture', enabled=True),
                Video(bvid='BVdigest', mid=1, title='fixture', publish_time=self.at, fetch_status='ok'),
                Summary(bvid='BVdigest', summary='fixture'),
                DailyDigest(digest_date=day, content={'summary': 'old'}), DirtyDigest(digest_date=day)])
            await db.commit()
        guard = OwnerGuard(self.engine, self.sessions, heartbeat_seconds=3600)
        await guard.start()
        token = owner_context.set(guard.ticket())
        async def response(_):
            await guard.acquire()
            return {'summary': 'new', 'overall_sentiment': 'neutral', 'overall_sentiment_desc': '',
                    'consensus': [], 'differences': [], 'key_topics': []}
        try:
            with patch.object(fetcher, 'generate_daily_digest', side_effect=response):
                with self.assertRaises(OwnershipLost):
                    await fetcher._generate_digest([{'mid': 1, 'name': 'fixture'}], {}, day)
        finally:
            owner_context.reset(token)
            await guard.close()
        async with self.sessions() as db:
            self.assertEqual((await db.get(DailyDigest, day)).content['summary'], 'old')
            self.assertIsNotNone(await db.get(DirtyDigest, day))

    async def test_stale_outbox_replay_preserves_intent_and_job_state(self):
        async with self.sessions() as db:
            db.add(FetchJob(id='stale-outbox', kind='fixture', status='running'))
            await db.commit()
        self.assertTrue(fetcher.outbox_save('stale-outbox', 'completed', {'processed': 1}))
        guard = OwnerGuard(self.engine, self.sessions, heartbeat_seconds=3600)
        await guard.start()
        old = guard.ticket()
        await guard.acquire()
        token = owner_context.set(old)
        try:
            with self.assertRaises(OwnershipLost):
                await fetcher.outbox_replay_on_startup()
        finally:
            owner_context.reset(token)
            await guard.close()
        async with self.sessions() as db:
            self.assertEqual((await db.get(FetchJob, 'stale-outbox')).status, 'running')
        self.assertEqual([entry['job_id'] for entry in fetcher.outbox_pending()], ['stale-outbox'])

    async def test_digest_model_request_uses_the_shared_daily_limit(self):
        from src import db as database
        config._CONFIG.setdefault('limits', {})['daily_llm_calls'] = 1
        guard = OwnerGuard(self.engine, self.sessions, heartbeat_seconds=3600)
        await guard.start()
        token = owner_context.set(guard.ticket())
        try:
            await workflow.reserve_global_llm(self.sessions)
            with patch.object(database, 'async_session', self.sessions), patch.object(analyzer.httpx, 'AsyncClient') as network:
                with self.assertRaisesRegex(WorkflowError, 'llm_budget'):
                    await analyzer._call_llm('fixture digest')
                network.assert_not_called()
        finally:
            owner_context.reset(token)
            await guard.close()
        async with self.sessions() as db:
            self.assertEqual((await db.get(DailyUsage, self.at.date())).llm_calls, 1)

    async def test_stale_job_registration_releases_the_local_lock(self):
        guard = OwnerGuard(self.engine, self.sessions, heartbeat_seconds=3600)
        await guard.start()
        old = guard.ticket()
        await guard.acquire()
        token = owner_context.set(old)
        worker = AsyncMock()
        try:
            with self.assertRaises(OwnershipLost):
                await main._submit_job('fixture', worker)
        finally:
            owner_context.reset(token)
            await guard.close()
        worker.assert_not_awaited()
        self.assertFalse(main._job_lock.locked())
        self.assertEqual(await self.count(FetchJob), 0)

    async def test_long_transcript_tail_enters_a_model_request(self):
        text='A'*8000+'CRITICAL_TAIL_FACT'
        model=AsyncMock(return_value=json.dumps(good_analysis()))
        with patch.object(analyzer,'_call_llm',model):
            result=await analyzer.analyze_video('fixture',text,[],[],900)
        self.assertFalse(result.get('analysis_failed',False))
        self.assertTrue(any('CRITICAL_TAIL_FACT' in call.args[0] for call in model.await_args_list))

    async def test_failed_middle_file_slice_never_reaches_asr(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('ffmpeg and ffprobe required for real media validation')
        source=Path(self.temp.name)/'source.wav'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=7','-y',str(source)],check=True,capture_output=True)
        config._CONFIG['asr']={'chunk_seconds':3}
        real_spawn=asyncio.create_subprocess_exec
        async def spawn(*args,**kwargs):
            if args[0]=='ffmpeg' and '-ss' in args and args[args.index('-ss')+1]=='3':
                return SimpleNamespace(returncode=1,wait=AsyncMock(return_value=1))
            return await real_spawn(*args,**kwargs)
        client=SimpleNamespace(get_audio_url=AsyncMock(return_value='mock://audio'),download_audio=AsyncMock(return_value=str(source)))
        with patch.object(asyncio,'create_subprocess_exec',spawn),patch.object(transcript,'_call_asr',AsyncMock()) as asr:
            with self.assertRaisesRegex(WorkflowError,'incomplete_audio'):
                await transcript._asr_transcribe(client,'BVnew',77,7)
        asr.assert_not_awaited()

    async def test_long_video_policy_does_not_consume_failure_attempts(self):
        await self.admit()
        client=self.client()
        client.get_video_info.return_value={'cid':77,'duration':4000}
        with patch.object(fetcher,'fetch_transcript',AsyncMock(side_effect=WorkflowError('policy_long_video','asr'))):
            result=await fetcher._process_video(client,1,self.payload())
        self.assertEqual(result['status'],'skipped')
        async with self.sessions() as session:
            work=await session.get(VideoWork,'BVnew')
            self.assertEqual(work.state,'skipped')
            self.assertEqual((await session.get(WorkStage,('BVnew','asr'))).attempts,0)

    async def test_successful_asr_segments_are_reused_after_later_failure(self):
        await self.admit()
        async with self.sessions() as session:
            work=await session.get(VideoWork,'BVnew');work.cid=77
            await session.commit()
        context=workflow.WorkContext(self.sessions,'BVnew',1,cid=77)
        context.policy={'asr_model':'test-model'}
        config._CONFIG['asr']={'chunk_seconds':3}
        def source():
            path=Path(self.temp.name)/'audio.bin';path.write_bytes(b'identical original audio');return str(path)
        async def slices(*args):
            paths=[]
            for i in range(3):
                path=Path(self.temp.name)/f'chunk{i}.mp3';path.write_bytes(f'chunk-{i}'.encode());paths.append(str(path))
            return paths
        client=SimpleNamespace(get_audio_url=AsyncMock(return_value='mock://audio'),download_audio=AsyncMock(side_effect=lambda _:source()))
        token=work_context.set(context)
        try:
            with patch.object(transcript,'_to_mp3_path',AsyncMock(side_effect=lambda path:path)),patch.object(transcript,'_probe_duration',AsyncMock(return_value=7.0)),patch.object(transcript,'_split_audio_from_file',slices),patch.object(transcript,'_call_asr',AsyncMock(side_effect=['one',None,'two','three'])) as asr:
                with self.assertRaisesRegex(ValueError,'2/3 failed'):
                    await transcript._asr_transcribe(client,'BVnew',77,7)
                result=await transcript._asr_transcribe(client,'BVnew',77,7)
            self.assertEqual(result['text'],'one two three')
            self.assertEqual(asr.await_count,4)
            self.assertEqual(await self.count(ASRSegment),3)
        finally:
            work_context.reset(token)
