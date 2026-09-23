"""可重复扫描/同步基准；仅操作新建临时目录，不读取用户文件。
用 --source-parent / --target-parent 放在本地或挂载共享，组成 A/B/C/D。
真实网络请求数需外部 SMB/NFS 抓包；本脚本不伪造网络计数。
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize
from standalone.scanner import get_scanner,ScanFields,ScanMetrics

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files',type=int,default=10000)
    parser.add_argument('--directories',type=int,default=1)
    parser.add_argument('--bytes',type=int,default=16)
    parser.add_argument('--source-parent');parser.add_argument('--target-parent')
    parser.add_argument('--workers',default='1,4,8,16,32')
    parser.add_argument('--backends',default='portable,auto')
    parser.add_argument('--strategies',default='COPY_ALL,SKIP_EXISTING,COMPARE_METADATA')
    parser.add_argument('--copy',action='store_true',help='实际复制 COPY_ALL；默认演练')
    parser.add_argument('--output')
    args=parser.parse_args()
    if args.files<1 or args.directories<1:parser.error('数量必须为正数')
    records=[]
    with tempfile.TemporaryDirectory(prefix='file-sync-bench-src-',dir=args.source_parent) as a,tempfile.TemporaryDirectory(prefix='file-sync-bench-dst-',dir=args.target_parent) as b:
        src=Path(a).resolve();dst=Path(b).resolve()
        for n in range(args.directories):
            (src/str(n)).mkdir();(dst/str(n)).mkdir()
        payload=b'x'*args.bytes
        for n in range(args.files):
            relative=Path(str(n%args.directories))/str(n)
            for root in (src,dst):
                (root/relative).write_bytes(payload);os.utime(root/relative,ns=(1_000_000_000_000,1_000_000_000_000))
        for backend in args.backends.split(','):
            for fields in (ScanFields.NAME_TYPE,ScanFields.METADATA):
                metrics=ScanMetrics();start=time.perf_counter();cpu=time.process_time();count=0
                for n in range(args.directories):
                    count+=sum(1 for _ in get_scanner(backend).scan(src/str(n),fields,metrics))
                elapsed=time.perf_counter()-start
                row=dict(kind='scanner',backend=backend,fields=fields.name,entries=count,seconds=elapsed,entriesPerSecond=count/elapsed,cpuSeconds=time.process_time()-cpu,metrics=metrics.snapshot())
                records.append(row);print(json.dumps(row),flush=True)
            for workers in map(int,args.workers.split(',')):
                for strategy in args.strategies.split(','):
                    start=time.perf_counter();cpu=time.process_time()
                    result=synchronize(dict(source=str(src),target=str(dst),syncStrategy=strategy,scannerBackend=backend,scanWorkers=workers,dryRun=not args.copy,parallelism=4,batchFiles=100),lambda event:None)
                    elapsed=time.perf_counter()-start
                    row=dict(kind='pipeline',backend=backend,workers=workers,strategy=strategy,seconds=elapsed,filesPerSecond=args.files/elapsed,cpuSeconds=time.process_time()-cpu,copyMBps=result['bytes']/elapsed/1_000_000 if args.copy else None,result=result)
                    try:
                        import resource
                        row['processPeakRssBytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
                    except ImportError:row['processPeakRssBytes']=None
                    records.append(row);print(json.dumps(row),flush=True)
    report=dict(platform=sys.platform,python=sys.version,files=args.files,directories=args.directories,copy=args.copy,networkCalls=None,notes='CPU 为进程累计差值，RSS 为整个进程峰值，包含数据准备；热缓存，无 API 日志入库开销；networkCalls 必须外部观测。',records=records)
    if args.output:Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
