"""单机入口：python backend/standalone_app.py。"""
from pathlib import Path
import argparse
import yaml
import uvicorn
from standalone.api import create_app

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description='SQLite 免登录单机版')
    parser.add_argument('--config', type=Path, default=ROOT / 'env/standalone/application.yml')
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
    host = config['app'].get('host', '127.0.0.1')
    if host not in ('127.0.0.1', '::1', 'localhost'):
        parser.error('免登录单机版只允许绑定回环地址')
    app = create_app(ROOT / config['database']['path'])
    uvicorn.run(app, host=host, port=int(config['app'].get('port', 9098)))

if __name__ == '__main__':
    main()
