"""在隔离临时数据库启动实际服务，验证健康版本；不使用用户任务。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp).resolve()
        config = path / 'application.yml'
        config.write_text('app:\n  host: 127.0.0.1\n  port: '+str(port)+'\ndatabase:\n  path: '+json.dumps(str(path/'test.sqlite3'))+'\n', encoding='utf-8')
        with (path/'runtime.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-I', str(ROOT/'backend/standalone_app.py'), '--config', str(config)], cwd=path, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ,PYTHONUTF8='1'))
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError((path/'runtime.log').read_text(encoding='utf-8',errors='replace'))
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1) as response:
                            health=json.load(response)
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('服务未在10秒内就绪')
                assert health['releaseVersion'] == (ROOT/'VERSION').read_text().strip()
                assert health['database'] == 'sqlite'
                print('PASS: 独立目录启动、隔离SQLite和版本健康检查')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()

if __name__ == '__main__':
    main()
