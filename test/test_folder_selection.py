"""验证目录浏览、多组目录执行及跨映射冲突保护。"""
import sys
import json
from unittest.mock import patch
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from standalone.api import create_app

class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for name in ('源 一', '源二'):
            (self.root / name).mkdir()
            (self.root / name / '文件.txt').write_text(name)
        self.client = TestClient(create_app(self.root / 'state/db.sqlite3'))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {'X-Sync-Local':'1'}
        self.mappings = [dict(source=str(self.root / name), target=str(self.root / ('目标'+str(i)))) for i,name in enumerate(('源 一','源二'))]

    def test_browse_directories_only_and_missing(self):
        r=self.client.get('/api/directories', params={'path':str(self.root / '源 一')})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['directories'],[])
        self.assertEqual(r.json()['parent'],str(self.root))
        r=self.client.get('/api/directories',params={'path':str(self.root)})
        self.assertIn('源 一',[item['name'] for item in r.json()['directories']])
        self.assertEqual(self.client.get('/api/directories',params={'path':str(self.root/'missing')}).status_code,404)
        self.assertEqual(self.client.get('/api/directories',params={'path':'relative'}).status_code,422)
        self.assertEqual(self.client.get('/api/directories',params={'path':str(self.root/'state')}).status_code,403)

    def test_multi_run_and_persistence(self):
        r=self.client.post('/api/tasks',headers=self.headers,json={'name':'多目录','pathMappings':self.mappings})
        self.assertEqual(r.status_code,200,r.text)
        tid=r.json()['id']
        self.assertEqual(self.client.get('/api/tasks').json()[0]['pathMappings'],self.mappings)
        r=self.client.post(f'/api/tasks/{tid}/run',headers=self.headers,json={'dryRun':False})
        self.assertEqual(r.status_code,202,r.text)
        for _ in range(100):
            row=self.client.get('/api/runs').json()[0]
            if row['status']!='running':break
            time.sleep(.02)
        self.assertEqual(row['status'],'success',row)
        for m in self.mappings:
            self.assertEqual((Path(m['target'])/'文件.txt').read_text(),Path(m['source']).name)
        events=self.client.get(f"/api/runs/{row['id']}/events").json()
        self.assertTrue(any('mappingIndex' in e['detail'] for e in events))

    def test_overlap_between_mappings_rejected(self):
        for target in (self.mappings[0]['target'],self.mappings[0]['target']+'/child',self.mappings[0]['source']):
            mappings=[self.mappings[0],dict(self.mappings[1],target=target)]
            r=self.client.post('/api/tasks',headers=self.headers,json={'name':'冲突','pathMappings':mappings})
            self.assertEqual(r.status_code,422,r.text)
        self.assertEqual(self.client.post('/api/tasks',headers=self.headers,json={'name':'空','pathMappings':[]}).status_code,422)

    def test_browse_pagination_and_files(self):
        collected = []
        offset = 0
        while offset is not None:
            response = self.client.get('/api/directories', params={'path': str(self.root), 'limit': 1, 'offset': offset})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            collected.extend(item['name'] for item in data['directories'])
            offset = data['nextOffset']
        self.assertCountEqual(collected, ['源 一', '源二'])
        response = self.client.get('/api/directories', params={'path': str(self.root/'源 一/文件.txt')})
        self.assertEqual(response.status_code, 422)
        self.assertTrue(self.client.get('/api/directories').json()['directories'])

    def test_multi_preview_does_not_create_targets(self):
        response = self.client.post('/api/tasks', headers=self.headers, json={'name': '演练', 'pathMappings': self.mappings})
        tid = response.json()['id']
        response = self.client.post(f'/api/tasks/{tid}/run', headers=self.headers, json={'dryRun': True})
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(100):
            row = self.client.get('/api/runs').json()[0]
            if row['status'] != 'running':
                break
            time.sleep(.02)
        self.assertEqual(row['status'], 'preview', row)
        for mapping in self.mappings:
            self.assertFalse(Path(mapping['target']).exists())

    def test_group_failure_stops_later_groups_and_logs_paths(self):
        response = self.client.post('/api/tasks', headers=self.headers, json={'name': '失败', 'pathMappings': self.mappings})
        tid = response.json()['id']
        with patch('standalone.api.synchronize', side_effect=OSError('模拟共享断开')) as sync:
            response = self.client.post(f'/api/tasks/{tid}/run', headers=self.headers, json={'dryRun': False})
            self.assertEqual(response.status_code, 202, response.text)
            for _ in range(100):
                row = self.client.get('/api/runs').json()[0]
                if row['status'] != 'running':
                    break
                time.sleep(.02)
            self.assertEqual(row['status'], 'failed', row)
            self.assertEqual(sync.call_count, 1)
        events = self.client.get(f"/api/runs/{row['id']}/events").json()
        failures = [json.loads(e['detail']) for e in events if json.loads(e['detail'])['action'] == 'mapping_failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['mappingIndex'], 1)
        self.assertEqual(failures[0]['source'], self.mappings[0]['source'])
        for mapping in self.mappings:
            self.assertFalse(Path(mapping['target']).exists())

    def test_directory_search_sort_before_pagination(self):
        folder=self.root/'browse';folder.mkdir()
        for name in ['目录10','目录2','目录1','REPORT9','report3','其他']:
            (folder/name).mkdir()
        response=self.client.get('/api/directories',params={'path':str(folder),'query':'目录','limit':2})
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertEqual([d['name'] for d in data['directories']],['目录1','目录2'])
        self.assertEqual(data['total'],3)
        response=self.client.get('/api/directories',params={'path':str(folder),'query':'目录','limit':2,'offset':2})
        self.assertEqual([d['name'] for d in response.json()['directories']],['目录10'])
        self.assertIsNone(response.json()['nextOffset'])
        response=self.client.get('/api/directories',params={'path':str(folder),'query':'REPORT','sort':'name_desc'})
        self.assertEqual([d['name'] for d in response.json()['directories']],['REPORT9','report3'])

    def test_directory_time_sort_and_empty_search(self):
        import os
        folder=self.root/'times';folder.mkdir()
        for i,name in enumerate(['old','new']):
            (folder/name).mkdir();os.utime(folder/name,(1000+i,1000+i))
        response=self.client.get('/api/directories',params={'path':str(folder),'sort':'modified_desc','limit':1})
        self.assertEqual(response.json()['directories'][0]['name'],'new')
        self.assertEqual(response.json()['directories'][0]['modified'],1001)
        response=self.client.get('/api/directories',params={'path':str(folder),'query':'missing'})
        self.assertEqual(response.json()['total'],0)
        self.assertEqual(response.json()['directories'],[])
        self.assertEqual(self.client.get('/api/directories',params={'path':str(folder),'sort':'bad'}).status_code,422)
