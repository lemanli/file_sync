"""单向仅大小比较基准：临时目录、真实枚举、目标 Path.stat 调用计数。"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',default=str(Path(__file__).resolve().parents[1]/'backend'))
    parser.add_argument('--files',type=int,default=100000)
    parser.add_argument('--memory-mb',type=int,default=256)
    parser.add_argument('--output')
    args=parser.parse_args()
    sys.path.insert(0,args.backend)
    from standalone.engine import synchronize
    with tempfile.TemporaryDirectory(prefix='sync-size-benchmark-') as folder:
        root=Path(folder).resolve();src=root/'source';dst=root/'target';src.mkdir();dst.mkdir()
        for n in range(args.files):
            (src/str(n)).write_bytes(b'a'*128)
            (dst/str(n)).write_bytes(b'b'*128)
        calls=0;original=Path.stat
        def query(path,*a,**kw):
            nonlocal calls
            if path.parent==dst and kw.get('follow_symlinks',True):calls+=1
            return original(path,*a,**kw)
        started=time.perf_counter()
        with patch.object(Path,'stat',query):
            result=synchronize(dict(source=str(src),target=str(dst),syncStrategy='AUTO',comparisonMode='size',
                                    parallelism=32,batchFiles=500,scanWorkers=8,scanMemoryMb=args.memory_mb),lambda event:None)
        elapsed=time.perf_counter()-started
        assert result['skipped']==args.files and result['copied']==0,result
        report=dict(files=args.files,seconds=elapsed,filesPerSecond=args.files/elapsed,
                    targetFilePathStatCalls=calls,scanMemoryMb=args.memory_mb,result=result)
        try:
            import resource
            report['processPeakRssBytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
        except ImportError:pass
        text=json.dumps(report,ensure_ascii=False,indent=2)
        if args.output:Path(args.output).write_text(text+'\n',encoding='utf-8')
        print(text)


if __name__=='__main__':main()
