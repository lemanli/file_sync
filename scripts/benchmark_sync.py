"""在新临时目录对比并发，不触碰业务文件；结果输出为 JSON。"""
import argparse
import json
import platform
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from standalone.engine import synchronize

parser = argparse.ArgumentParser(description='本地同步基准：全量、增量、大文件')
parser.add_argument('--files', type=int, default=1000)
parser.add_argument('--size', type=int, default=4096)
parser.add_argument('--large-mb', type=int, default=64)
args = parser.parse_args()
if min(args.files, args.size, args.large_mb) < 1:
    parser.error('参数必须为正整数')
results = []
with tempfile.TemporaryDirectory(prefix='sync-benchmark-') as directory:
    root = Path(directory).resolve()
    source = root / 'source'
    source.mkdir()
    for i in range(args.files):
        (source / str(i)).write_bytes(b'x' * args.size)
    for workers in [1, 2, 4, 8]:
        cfg = dict(source=str(source), target=str(root / str(workers)), parallelism=workers)
        for stage in ['full', 'unchanged', 'changed']:
            if stage == 'changed':
                (source / '0').write_bytes(b'y' * (args.size + workers))
            result = synchronize(cfg, lambda event: None)
            result.update(workers=workers, stage=stage)
            elapsed = max(result['durationSeconds'], 0.0001)
            result['filesPerSecond'] = round(result['scanned'] / elapsed, 2)
            result['MiBPerSecond'] = round(result['bytes'] / elapsed / 1024**2, 2)
            results.append(result)
    large = root / 'large'
    large.mkdir()
    with (large / 'file.bin').open('wb') as stream:
        for _ in range(args.large_mb):
            stream.write(b'z' * 1024**2)
    result = synchronize(dict(source=str(large), target=str(root / 'large-dst'), parallelism=1, largeThresholdMb=1), lambda e: None)
    result.update(workers=1, stage='large')
    results.append(result)
print(json.dumps(dict(platform=platform.platform(), python=platform.python_version(), files=args.files, fileBytes=args.size, largeMiB=args.large_mb, results=results), ensure_ascii=False, indent=2))
