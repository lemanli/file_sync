"""固定数据目录、异常退出恢复及独占锁的跨平台验证。"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from standalone.storage import DatabaseLock, database_path, prepare_database, migrate
from standalone.api import create_app
from fastapi.testclient import TestClient


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()

    def old_database(self):
        database=self.root/'old/file_sync.sqlite3'
        with TestClient(create_app(database)):
            pass
        with closing(sqlite3.connect(database)) as db,db:
            db.execute("INSERT INTO tasks(config) VALUES(?)",(json.dumps({'name':'旧任务','source':'/old/source','target':'/old/target'}),))
            db.execute("INSERT INTO runs(task_id,status,started,snapshot) VALUES(1,'running','old','{}')")
        return database

    def test_shared_directory_and_legacy_path(self):
        shared=self.root/'shared'
        config={'database':{'directory':str(shared),'path':'ignored.db'}}
        self.assertEqual(database_path(self.root/'version1',config),shared/'file_sync.sqlite3')
        self.assertEqual(database_path(self.root/'version2',config),shared/'file_sync.sqlite3')
        self.assertEqual(database_path(self.root,{'database':{'path':'legacy/db'}}),self.root/'legacy/db')

    def test_config_migrates_once_recovers_stale_and_preserves_source(self):
        old=self.old_database()
        config={'database':{'directory':'shared','migrate_from':str(old.parent)}}
        new=prepare_database(self.root,config)
        with closing(sqlite3.connect(new)) as db:
            self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'interrupted')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        with closing(sqlite3.connect(old)) as db:
            self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'running')
        # 再次启动不从旧库覆盖用户的新数据。
        with closing(sqlite3.connect(new)) as db,db:db.execute("UPDATE tasks SET config='{}'")
        self.assertEqual(prepare_database(self.root,config),new)
        with closing(sqlite3.connect(new)) as db:self.assertEqual(db.execute('SELECT config FROM tasks').fetchone()[0],'{}')

    def test_live_source_and_destination_locks_prevent_migration(self):
        old=self.old_database();new=self.root/'new/file_sync.sqlite3'
        with DatabaseLock(old):
            with self.assertRaisesRegex(RuntimeError,'正在被'):migrate(old,new)
        with DatabaseLock(new):
            with self.assertRaisesRegex(RuntimeError,'正在被'):migrate(old,new)
        self.assertFalse(new.exists())

    def test_process_termination_releases_lock(self):
        database=self.root/'shared/file_sync.sqlite3'
        code="import sys;sys.path.insert(0,sys.argv[1]);from standalone.storage import DatabaseLock;lock=DatabaseLock(sys.argv[2]);lock.__enter__();print('locked',flush=True);sys.stdin.read()"
        process=subprocess.Popen([sys.executable,'-c',code,str(ROOT/'backend'),str(database)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            with self.assertRaises(RuntimeError):
                with DatabaseLock(database):pass
        finally:
            process.terminate();process.wait(timeout=5)
            process.stdin.close();process.stdout.close();process.stderr.close()
        with DatabaseLock(database):pass

    def test_shared_startup_recovers_history_with_log_once(self):
        old=self.old_database()
        for _ in range(2):
            with TestClient(create_app(old)) as client:
                self.assertEqual(client.get('/api/runs').json()[0]['status'],'interrupted')
        with closing(sqlite3.connect(old)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)

    def test_migrate_only_reads_configuration(self):
        old=self.old_database();destination=self.root/'configured'
        config=self.root/'app.yml'
        config.write_text('app:\n  host: 127.0.0.1\ndatabase:\n  directory: '+json.dumps(str(destination))+'\n  migrate_from: '+json.dumps(str(old.parent))+'\n',encoding='utf-8')
        result=subprocess.run([sys.executable,'-I',str(ROOT/'backend/standalone_app.py'),'--config',str(config),'--migrate-only'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((destination/'file_sync.sqlite3').is_file())

    def test_launcher_refuses_locked_shared_database_before_recovery(self):
        old=self.old_database();config=self.root/'shared.yml'
        config.write_text('app:\n  host: 127.0.0.1\ndatabase:\n  directory: '+json.dumps(str(old.parent))+'\n',encoding='utf-8')
        with DatabaseLock(old):
            result=subprocess.run([sys.executable,'-I',str(ROOT/'backend/standalone_app.py'),'--config',str(config),'--migrate-only'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        with closing(sqlite3.connect(old)) as db:self.assertEqual(db.execute('SELECT status FROM runs').fetchone()[0],'running')

    def test_isolated_entry_migration_with_non_unicode_output_pipe(self):
        old=self.old_database();config=self.root/'encoding.yml'
        config.write_text('app:\n  host: 127.0.0.1\ndatabase:\n  directory: '+json.dumps(str(self.root/'new'))+'\n  migrate_from: '+json.dumps(str(old))+'\n',encoding='utf-8')
        code="import sys,runpy;sys.stdout.reconfigure(encoding='cp1252',errors='strict');sys.stderr.reconfigure(encoding='cp1252',errors='strict');sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
        result=subprocess.run([sys.executable,'-I','-c',code,str(ROOT/'backend/standalone_app.py'),'--config',str(config),'--migrate-only'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertIn('数据准备完成',result.stdout.decode('utf-8'))
