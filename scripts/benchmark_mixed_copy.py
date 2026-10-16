"""在临时目录验证混合文件实际复制；可指定旧版 backend 做同机对照。"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default=str(Path(__file__).resolve().parents[1] / 'backend'))
    parser.add_argument('--files', type=int, default=2000)
    parser.add_argument('--parallelism', type=int, default=32)
    parser.add_argument('--batch', type=int, default=500)
    parser.add_argument('--output')
    args = parser.parse_args()
    sys.path.insert(0, args.backend)
    from standalone.engine import synchronize
    lock = threading.Lock(); active = set(); peak = 0
    def progress(kind, **data):
        nonlocal peak
        with lock:
            if kind == 'file':
                active.add(threading.get_ident()); peak = max(peak, len(active))
            elif kind == 'file_done':
                active.discard(threading.get_ident())
    with tempfile.TemporaryDirectory(prefix='sync-mixed-') as folder:
        root = Path(folder).resolve(); source = root / 'source'; source.mkdir()
        total = 0
        for n in range(args.files):
            payload = b'x' * (2 * 1024 * 1024 if n % 200 == 0 else 4096)
            (source / str(n)).write_bytes(payload); total += len(payload)
        start = time.perf_counter()
        result = synchronize(dict(source=str(source), target=str(root / 'target'),
                                  syncStrategy='COMPARE_METADATA', scanWorkers=16,
                                  parallelism=args.parallelism, batchFiles=args.batch,
                                  largeThresholdMb=1, maxRetries=0, _progress=progress), lambda event: None)
        elapsed = time.perf_counter() - start
        assert result['copied'] == args.files and result['bytes'] == total, result
        report = dict(files=args.files, bytes=total, seconds=elapsed, filesPerSecond=args.files / elapsed,
                      peakActiveFiles=peak, parallelism=args.parallelism, batchFiles=args.batch,
                      copyMiBps=total / elapsed / 1024 / 1024, result=result)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(text + '\n', encoding='utf-8')
        print(text)


if __name__ == '__main__':
    main()
