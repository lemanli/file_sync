"""升级后普通刷新必须加载对应网页资源，避免旧脚本误判新版接口。"""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi.testclient import TestClient
from standalone.api import create_app

class AssetCacheTests(unittest.TestCase):
    def test_html_assets_and_health_do_not_reuse_old_cache(self):
        with tempfile.TemporaryDirectory() as folder,TestClient(create_app(Path(folder)/'db')) as client:
            for path in ('/','/api/health','/assets/app.js','/assets/app.css'):
                response=client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.headers.get('cache-control'),'no-store',path)
            html=client.get('/').text
            self.assertNotIn('reports-20260920',html)
            for name in ('app.js','app.css'):
                content=(Path(__file__).resolve().parents[1]/'backend/standalone/assets'/name).read_bytes()
                self.assertIn('/assets/'+name+'?v='+hashlib.sha256(content).hexdigest()[:16],html)
