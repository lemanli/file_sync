"""在临时目录对比预检耗时和 lstat 调用数，不触碰业务文件。"""
import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import types
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.engine import synchronize
from standalone.progress import Progress


class ScanFinished(BaseException):
    pass


def main():
    parser=argparse.ArgumentParser(description='扫描基准；可指定本项目旧安装包进行对照')
    parser.add_argument('--files',type=int,default=20000)
    parser.add_argument('--baseline-zip',type=Path)
    args=parser.parse_args()
    if args.files<1:parser.error('files 必须大于 0')
    versions=[('current',synchronize)]
    if args.baseline_zip:
        with zipfile.ZipFile(args.baseline_zip) as archive:
            name=next(name for name in archive.namelist() if name.endswith('/backend/standalone/engine.py'))
            module=types.ModuleType('standalone._benchmark_baseline')
            exec(compile(archive.read(name),name,'exec'),module.__dict__)
            versions.insert(0,('baseline',module.synchronize))
    results=[]
    with tempfile.TemporaryDirectory(prefix='scan-benchmark-') as folder:
        root=Path(folder).resolve();source=root/'source';source.mkdir()
        for n in range(args.files):
            directory=source/str(n%100);directory.mkdir(exist_ok=True)
            (directory/str(n)).touch()
        for name,run in versions:
            timings=[];counts=[];scanned=0
            for _ in range(3):
                counter=[0];original=Path.lstat;progress=Progress(1)
                def lstat(path,*args,**kwargs):
                    counter[0]+=1
                    return original(path,*args,**kwargs)
                def update(kind,**data):
                    if kind=='phase' and data.get('phase')=='syncing':raise ScanFinished()
                    progress.update(kind,**data)
                start=time.perf_counter()
                with patch.object(Path,'lstat',lstat):
                    try:run(dict(source=str(source),target=str(root/'unused-target'),dryRun=True,_progress=update),lambda event:None)
                    except ScanFinished:pass
                timings.append(time.perf_counter()-start);counts.append(counter[0]);scanned=progress.snapshot()['scannedEntries']
            elapsed=statistics.median(timings)
            results.append(dict(version=name,medianSeconds=round(elapsed,4),scannedEntries=scanned,entriesPerSecond=round(scanned/elapsed),lstatCalls=counts))
    print(json.dumps(dict(platform=platform.platform(),files=args.files,results=results,note='本机临时目录预检基准；基线使用当前公共辅助模块，不代表网络共享或实际复制吞吐'),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
