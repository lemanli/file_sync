"""旧库迁移兼容命令；不传路径时读取 application.yml 的数据库配置。"""
import argparse
from pathlib import Path
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from standalone.storage import migrate, prepare_database


def main():
    parser = argparse.ArgumentParser(description='迁移旧版任务和日志；或按配置复用固定数据目录')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        if args.source:
            destination = args.destination or ROOT/'data/standalone/file_sync.sqlite3'
            print('迁移完成：'+str(destination)+'；'+str(migrate(args.source,destination)))
        else:
            if args.destination:
                parser.error('--destination 需要同时指定 --source')
            import yaml
            config=yaml.safe_load((ROOT/'env/standalone/application.yml').read_text(encoding='utf-8-sig'))
            print('数据配置已就绪：'+str(prepare_database(ROOT,config)))
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error)+'\n')


if __name__ == '__main__':
    main()
