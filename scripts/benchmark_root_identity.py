"""只读比较 Windows 根目录身份查询；不扫描目录、不创建文件。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.root_identity import root_stat


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--checks',type=int,default=320)
    parser.add_argument('--workers',type=int,nargs='+',default=[1,8,32])
    parser.add_argument('--rounds',type=int,default=2)
    args=parser.parse_args()
    if os.name!='nt':parser.error('请在 Windows 上测试真实 NAS 目录')
    if not 1<=args.checks<=100000 or not 1<=args.rounds<=20 or any(not 1<=v<=32 for v in args.workers):
        parser.error('checks 应为 1–100000，rounds 为 1–20，workers 为 1–32')
    root=args.directory.absolute();baseline=root.lstat();expected=(baseline.st_dev,baseline.st_ino)
    if not root.is_dir():parser.error('必须选择目录')
    records=[]
    for workers in args.workers:
        for turn in range(args.rounds):
            methods=[('python_lstat',lambda counts:root.lstat()),('native_identity',lambda counts:root_stat(root,counts))]
            for name,query in methods[::1 if turn%2==0 else -1]:
                def batch(count):
                    counters={}
                    for _ in range(count):
                        info=query(counters)
                        if (info.st_dev,info.st_ino)!=expected:
                            raise RuntimeError('目录身份不一致，停止基准')
                    return counters
                started=time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    # 每个 worker 一个有界循环，不为每次查询创建 Future。
                    futures=[pool.submit(batch,args.checks//workers+(i<args.checks%workers)) for i in range(workers)]
                    counters={}
                    for future in futures:
                        for key,value in future.result().items():counters[key]=counters.get(key,0)+value
                seconds=time.perf_counter()-started
                records.append(dict(method=name,workers=workers,round=turn+1,checks=args.checks,
                                    seconds=seconds,checksPerSecond=args.checks/seconds,counters=counters))
    print(json.dumps(records,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
