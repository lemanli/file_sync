"""安装源选择与有限回退，测试不访问网络。"""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('source_deploy',Path(__file__).resolve().parents[1]/'scripts/deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

class SourceTests(unittest.TestCase):
    def test_setting_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'env').mkdir();(root/'env/install.ini').write_text('[pip]\nsource=tuna\n')
            with patch.dict(os.environ,{},clear=True):
                self.assertEqual(deploy.source_setting(root),'tuna')
            with patch.dict(os.environ,{'FILE_SYNC_PIP_SOURCE':'pypi'}):
                self.assertEqual(deploy.source_setting(root),'pypi')
                self.assertEqual(deploy.source_setting(root,'configured'),'configured')
    def test_existing_pip_environment_preserved(self):
        with patch.dict(os.environ,{'PIP_INDEX_URL':'https://private.invalid/simple'},clear=True),patch.object(deploy,'reachable_source') as probe:
            self.assertEqual(deploy.installation_sources(Path('python'),'auto'),[None]);probe.assert_not_called()
    def test_config_file_source_and_offline_detected(self):
        for setting in ["global.index-url='https://example.invalid/simple'","install.no-index='true'","global.find-links='D:/wheels'"]:
            with patch.dict(os.environ,{},clear=True),patch.object(deploy.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=setting)):
                self.assertTrue(deploy.pip_has_source(Path('python')))
    def test_auto_falls_back_to_reachable_mirror(self):
        with patch.object(deploy,'pip_has_source',return_value=False),patch.object(deploy,'reachable_source',side_effect=[False,True]):
            self.assertEqual(deploy.installation_sources(Path('python'),'auto'),[deploy.PACKAGE_SOURCES['tuna']])
    def test_all_unreachable_stops(self):
        with patch.object(deploy,'pip_has_source',return_value=False),patch.object(deploy,'reachable_source',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'均未通过'):deploy.installation_sources(Path('python'),'auto')
    def test_explicit_and_invalid_sources(self):
        with patch.object(deploy,'reachable_source') as probe:
            self.assertEqual(deploy.installation_sources(Path('python'),'tuna'),[deploy.PACKAGE_SOURCES['tuna']]);probe.assert_not_called()
        for invalid in ['http://bad/simple','https://user:secret@bad/simple','https://bad/simple?token=secret','unknown']:
            with self.assertRaises(ValueError):deploy.installation_sources(Path('python'),invalid)
    def test_install_retry_is_bounded_and_remembers_success(self):
        sources=list(deploy.PACKAGE_SOURCES.values())
        with patch.object(deploy.subprocess,'run',side_effect=[subprocess.CalledProcessError(1,'pip'),subprocess.CompletedProcess([],0)]) as run:
            self.assertEqual(deploy.install_requirements(Path('python'),Path('requirements.txt'),sources),[sources[1]])
            self.assertEqual(run.call_count,2)
        with patch.object(deploy.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'pip')) as run:
            with self.assertRaises(subprocess.CalledProcessError):deploy.install_requirements(Path('python'),Path('requirements.txt'),[None])
            self.assertEqual(run.call_count,1)
