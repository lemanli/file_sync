"""只读测试 Windows/NAS 路径检查；不创建、复制或删除目标文件。"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from standalone.path_attributes import windows_link, link_info


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--rounds',type=int,default=4)
    args=parser.parse_args()
    if os.name!='nt':parser.error('请在 Windows 上测试真实 NAS 路径')
    if args.samples<1 or args.rounds<1:parser.error('样本数和轮次必须大于零')
    paths=[]
    with os.scandir(args.directory.absolute()) as entries:
        for entry in entries:
            paths.append(Path(entry.path))
            if len(paths)>=args.samples:break
    if not paths:parser.error('目录没有条目，请选择含文件的目录')
    def legacy(path,counts):
        counts['lstatQueries']=counts.get('lstatQueries',0)+1
        try:return link_info(path.lstat())
        except FileNotFoundError:return False
    reports=[]
    for turn in range(args.rounds):
        # 交替测试顺序，降低后跑的一组独享缓存的偏差。
        methods=[('legacy',legacy),('attributes',windows_link)]
        for name,method in methods[::1 if turn%2==0 else -1]:
            counts={};started=time.perf_counter();links=0
            for path in paths:
                for candidate in (path,*path.parents):
                    links+=int(method(candidate,counts))
            reports.append(dict(round=turn+1,method=name,seconds=time.perf_counter()-started,
                                samples=len(paths),links=links,queries=counts))
    print(json.dumps(reports,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
