"""验证断网等待、次数用尽后暂停、手动恢复及复制阶段的短路。"""
import errno
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi.testclient import TestClient
from standalone.api import create_app
from standalone.preflight import check_destination
from standalone.recovery import network_error


class NetworkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.src=self.root/'src';self.src.mkdir()
        for n in range(3):(self.src/str(n)).write_text(str(n))
        self.dst=self.root/'dst';self.headers={'X-Sync-Local':'1'}

    def wait_status(self,client,status):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            run=client.get('/api/runs').json()[0]
            if run['status'] in status:return run
            time.sleep(.02)
        self.fail('等待状态超时：'+str(run))

    def start(self,client,**options):
        task=dict(name='网络恢复',source=str(self.src),target=str(self.dst),continueOnError=True,networkRetryCount=1,networkRetryMinutes=.01,parallelism=1,batchFiles=1,maxRetries=0,**options)
        tid=client.post('/api/tasks',json=task,headers=self.headers).json()['id']
        response=client.post(f'/api/tasks/{tid}/run',json={},headers=self.headers)
        self.assertEqual(response.status_code,202,response.text)
        return response.json()['runId']

    def test_exhaustion_stays_paused_then_admin_recovers(self):
        online=[False];calls=[]
        def probe(*args,**kwargs):
            calls.append(1)
            if not online[0]:raise OSError(errno.ENETUNREACH,'网络不可达')
            return check_destination(*args,**kwargs)
        with TestClient(create_app(self.root/'state/db')) as client,patch('standalone.api.check_destination',side_effect=probe):
            rid=self.start(client)
            run=self.wait_status(client,{'retry_wait'})
            self.assertEqual(run['recovery']['attempt'],1)
            run=self.wait_status(client,{'paused'})
            self.assertTrue(run['recovery']['requiresResume'])
            count=len(calls);time.sleep(.15);self.assertEqual(len(calls),count)
            self.assertFalse(self.dst.exists())
            online[0]=True
            client.post(f'/api/runs/{rid}/resume',json={},headers=self.headers)
            run=self.wait_status(client,{'success','failed'})
            self.assertEqual(run['status'],'success',run)
            self.assertEqual(len(list(self.dst.iterdir())),3)
            self.assertTrue(client.get(f'/api/runs/{rid}/events',params={'action':'network_retry_exhausted'}).json())

    def test_mid_copy_network_error_retries_group_without_losing_counts(self):
        calls=[0];original=shutil.copymode
        def copy_mode(*args,**kwargs):
            calls[0]+=1
            if calls[0]==2:raise TimeoutError(errno.ETIMEDOUT,'模拟短暂断网')
            return original(*args,**kwargs)
        with TestClient(create_app(self.root/'state/db')) as client,patch('standalone.engine.shutil.copymode',side_effect=copy_mode):
            rid=self.start(client)
            run=self.wait_status(client,{'success','failed'})
            self.assertEqual(run['status'],'success',run)
            result=json.loads(run['result']);self.assertEqual(result['copied'],3)
            self.assertEqual(result['bytes'],3)
            self.assertTrue(client.get(f'/api/runs/{rid}/events',params={'action':'network_retry'}).json())
            for n in range(3):self.assertEqual((self.dst/str(n)).read_text(),str(n))

    def test_permission_error_is_not_a_network_retry(self):
        self.assertTrue(network_error(TimeoutError('连接超时')))
        self.assertFalse(network_error(PermissionError(errno.EACCES,'拒绝访问')))
        self.assertFalse(network_error(ValueError('路径被替换为符号链接')))
        with TestClient(create_app(self.root/'state/db')) as client,patch('standalone.api.check_destination',side_effect=PermissionError(errno.EACCES,'拒绝访问')):
            self.start(client)
            self.assertEqual(self.wait_status(client,{'failed'})['status'],'failed')

    def test_shutdown_interrupts_long_retry_wait(self):
        with TestClient(create_app(self.root/'state/db')) as client,patch('standalone.api.check_destination',side_effect=OSError(errno.ENETDOWN,'断网')):
            self.start(client)
            self.wait_status(client,{'retry_wait'})
        with TestClient(create_app(self.root/'state/db')) as client:
            self.assertEqual(client.get('/api/runs').json()[0]['status'],'interrupted')


class DirectoryNetworkRecoveryTests(NetworkRecoveryTests):
    """同一故障契约也覆盖目录批量流水线。"""
    def start(self,client,**options):
        return super().start(client,syncStrategy='COMPARE_METADATA',**options)
