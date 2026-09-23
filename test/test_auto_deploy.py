"""自动部署分支验证；不修改真实环境、不联网安装。"""
import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('sync_deploy',Path(__file__).resolve().parents[1]/'scripts/deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

class AutoDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        source_patch=patch.object(deploy,'installation_sources',return_value=[None]);source_patch.start();self.addCleanup(source_patch.stop)
        for name in ['backend/standalone_app.py','backend/standalone/index.html','env/standalone/application.yml','backend/requirements-standalone.txt']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('sample==1\n')
        self.env=self.root/'.venv-standalone';self.env.mkdir()
    def test_healthy_environment_does_not_install(self):
        with patch.object(deploy,'interpreter_ready',return_value=True),patch.object(deploy,'standalone_ready',return_value=True),patch.object(deploy.subprocess,'run') as run:
            deploy.prepare_standalone(self.root);run.assert_not_called()
        self.assertTrue((self.env/'.sync-requirements.sha256').exists())
    def test_changed_requirements_install(self):
        (self.env/'.sync-requirements.sha256').write_text('old')
        with patch.object(deploy,'interpreter_ready',return_value=True),patch.object(deploy,'standalone_ready',return_value=True),patch.object(deploy.subprocess,'run') as run:
            deploy.prepare_standalone(self.root)
            self.assertTrue(any('install' in c.args[0] for c in run.call_args_list))
    def test_broken_modules_force_reinstall(self):
        with patch.object(deploy,'interpreter_ready',return_value=True),patch.object(deploy,'standalone_ready',side_effect=[False,False,True]),patch.object(deploy.subprocess,'run') as run:
            deploy.prepare_standalone(self.root)
            self.assertTrue(any('--force-reinstall' in c.args[0] for c in run.call_args_list))
    def test_foreign_environment_preserved(self):
        (self.env/'keep').write_text('preserve')
        def create(path):path.mkdir()
        with patch.object(deploy,'interpreter_ready',return_value=False),patch.object(deploy,'standalone_ready',return_value=True),patch('venv.EnvBuilder') as builder:
            builder.return_value.create.side_effect=create
            deploy.prepare_standalone(self.root)
        backup=list(self.root.glob('.venv-standalone.backup-*'))
        self.assertEqual(len(backup),1);self.assertEqual((backup[0]/'keep').read_text(),'preserve')
    def test_install_failure_does_not_mark_success(self):
        with patch.object(deploy,'interpreter_ready',return_value=True),patch.object(deploy,'standalone_ready',return_value=False),patch.object(deploy.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'pip')):
            with self.assertRaises(subprocess.CalledProcessError):deploy.prepare_standalone(self.root)
        self.assertFalse((self.env/'.sync-requirements.sha256').exists())
    def test_missing_project_file_stops(self):
        (self.root/'env/standalone/application.yml').unlink()
        with self.assertRaisesRegex(RuntimeError,'工程文件缺失'):deploy.prepare_standalone(self.root)

    def test_default_entry_prepares_standalone(self):
        with patch.object(deploy.sys,'argv',['deploy.py','--prepare-only']),patch.object(deploy,'prepare_standalone',return_value=Path('python')) as prepare,patch.object(deploy.subprocess,'run') as run:
            deploy.main()
            prepare.assert_called_once_with(deploy.ROOT,None,'venv');run.assert_not_called()

    def test_legacy_standalone_argument_still_works(self):
        with patch.object(deploy.sys,'argv',['deploy.py','standalone','--auto','--prepare-only']),patch.object(deploy,'prepare_standalone',return_value=Path('python')) as prepare:
            deploy.main();prepare.assert_called_once()

    def test_server_mode_rejected_before_changes(self):
        import contextlib
        import io
        with patch.object(deploy.sys,'argv',['deploy.py','server']),patch.object(deploy,'prepare_standalone') as prepare,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:deploy.main()
            self.assertEqual(error.exception.code,2);prepare.assert_not_called()

    def test_direct_reuses_interpreter_without_environment_changes(self):
        import sys
        with patch.object(deploy, 'standalone_ready', return_value=True), patch('venv.EnvBuilder') as builder:
            python = deploy.prepare_standalone(self.root, environment='direct')
        self.assertEqual(python, Path(sys.executable))
        builder.assert_not_called()
        self.assertFalse((self.env / '.sync-requirements.sha256').exists())

    def test_direct_install_uses_selected_pip_without_ensurepip(self):
        with patch.object(deploy, 'standalone_ready', side_effect=[False, True, True]), patch.object(deploy.subprocess, 'run') as run:
            run.return_value.returncode = 0
            deploy.prepare_standalone(self.root, environment='direct')
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(any('install' in cmd for cmd in commands))
        self.assertFalse(any('ensurepip' in cmd for cmd in commands))
        self.assertTrue(all(cmd[0] == deploy.sys.executable for cmd in commands))

    def test_runtime_config_and_override(self):
        (self.root / 'env/install.ini').write_text('[runtime]\nenvironment=direct\npython=custom python\n')
        with patch.dict(deploy.os.environ, {}, clear=True):
            self.assertEqual(deploy.runtime_setting(self.root, 'environment'), 'direct')
            self.assertEqual(deploy.runtime_setting(self.root, 'environment', 'venv'), 'venv')
            self.assertEqual(deploy.configured_python(self.root, 'custom python'), self.root / 'custom python')
