"""单机入口：python backend/standalone_app.py。"""
from pathlib import Path
import argparse
import sys
import sqlite3

# 目录安装或嵌入式 Python 可能启用隔离路径；显式加载随工程发布的模块。
BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))
import yaml
import uvicorn
from standalone.api import create_app
from standalone.storage import DatabaseLock, prepare_database

ROOT = Path(__file__).resolve().parents[1]

def main():
    # -I 会忽略 PYTHONUTF8；Windows 管道可能仍为 cp1252，显式统一中文输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description='SQLite 免登录单机版')
    parser.add_argument('--config', type=Path, default=ROOT / 'env/standalone/application.yml')
    parser.add_argument('--migrate-only', action='store_true', help='按数据库配置准备数据，不启动服务')
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding='utf-8-sig'))
    host = config['app'].get('host', '127.0.0.1')
    if host not in ('127.0.0.1', '::1', 'localhost'):
        parser.error('免登录单机版只允许绑定回环地址')
    try:
        database = prepare_database(ROOT, config)
        with DatabaseLock(database):
            app = create_app(database)
            if args.migrate_only:
                print('数据准备完成：'+str(database)+'；旧的未结束记录已标为中断。', flush=True)
                return
            uvicorn.run(app, host=host, port=int(config['app'].get('port', 9098)))
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error)+'\n')

if __name__ == '__main__':
    main()
