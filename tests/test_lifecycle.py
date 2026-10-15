"""Failure injection and history-admission tests. No paid or real upstream calls."""
import asyncio
from datetime import datetime, timedelta
import json
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

    async def test_credential_write_requires_separate_management_authorization(self):
        with patch.dict('os.environ',{'FIN_AGG_MANAGEMENT_KEY':'management-test-secret'}):
            response=await self.http.post('/api/bili/auth/sessions')
        self.assertEqual(response.status_code,403)
        self.assertEqual(await self.count(LoginSession),0)

    async def test_qr_candidate_is_verified_before_replacing_and_never_returned(self):
        from urllib.parse import urlencode
        secret='TEST_SESSION_NEVER_RETURN'
        upstream=[{'code':0,'data':{'url':'https://passport.bilibili.com/scan?qrcode_key=TEST_QR','qrcode_key':'TEST_QR'}},
            {'code':0,'data':{'code':0,'url':'https://passport.bilibili.com/callback?'+urlencode({'SESSDATA':secret,'bili_jct':'TEST_CSRF','DedeUserID':'1'})}},
            {'code':0,'data':{'mid':1,'isLogin':True}}]
        headers={'X-Management-Key':'management-test-secret'}
        with patch.dict('os.environ',{'FIN_AGG_MANAGEMENT_KEY':'management-test-secret','BILI_UID':'1'}),patch.object(bilibili.BiliClient,'_get',AsyncMock(side_effect=upstream)),patch.object(bilibili.BiliClient,'validate_sessdata',AsyncMock(return_value={'state':'valid'})):
            created=await self.http.post('/api/bili/auth/sessions',headers=headers)
            self.assertEqual(created.status_code,200)
            session=created.json()['data']
            completed=await self.http.post('/api/bili/auth/sessions/'+session['id']+'/poll',headers=headers,json={'nonce':session['nonce']})
        self.assertEqual(completed.status_code,200,completed.text)
        self.assertEqual(credential_store().current().version,2)
        self.assertNotIn(secret,completed.text)
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
