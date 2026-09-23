# 目录扫描优化结果（0.1.7）

## 结论与启用方式

在任务编辑页选择“扫描与同步策略”。本次以新增明确策略的方式接入目录流水线；旧任务仍为 AUTO，不会静默改变仅大小、SHA、仅更新和双向冲突语义。要让已有单向任务使用新架构，需要编辑选择新策略。

单向任务不建立持久索引、不依赖上一次运行。每次重新枚举目录。小目录使用临时 dict，超过 10,000 项才溢写到系统临时目录 SQLite；该文件仅为当前目录的内存替代，退出即删除。目录队列和删除候选也仅是当前执行的临时缓冲。双向计划未在本次改写。

| 策略 | 源字段 | 目标字段 | 行为 |
|---|---|---|---|
| COPY_ALL | 名称、类型 | 不枚举 | 同名文件也复制覆盖 |
| SKIP_EXISTING | 名称、类型 | 名称、类型 | 名称存在即跳过，包括同名目录；不读大小/时间/hash |
| COMPARE_METADATA | 名称、类型、大小、mtime_ns | 同左 | 在目录快照中比较，时间容差生效 |
| MIRROR | 同上 | 同上 | 复制成功后重新按目录生成临时删除计划，自底向上删除 |
| AUTO | 原逻辑 | 原逻辑 | 兼容大小/SHA/updateOnly/双向任务；仍有完整预检及逐文件查询 |

MIRROR 的额外末期扫描是有意保留：不能提前执行删除，也不能把源扫描失败视为“源不存在”。忽略条目与其父目录保留；任何扫描/复制失败禁止删除；删除前保留根身份、路径链以及源条目重新出现的安全检查。后者会产生源属性请求，不能称为零 metadata。

## 架构与平台

- `scanner/base.py` 定义字段、不可变轻量条目和线程安全计数；上层不引用平台 API。
- `portable.py` 用 os.scandir；需要属性时每个 DirEntry 只显式 stat 一次并复用。TYPE 模式不请求大小/时间，但 DT_UNKNOWN 可能让 CPython 隐式查询类型。
- `linux.py` 使用 CPython 已有的 libc readdir/getdents 通道。未增加重复的 ctypes getdents/statx 实现或 metadata 文件线程池。
- `macos.py` 用 256 KiB getattrlistbulk 缓冲，解析 returned attributes、逐条错误、名称引用、类型及可选大小/纳秒时间。只请求必要字段，不请求 owner/permissions/creation time。自动模式 NAME/TYPE 用本地实测更快的 scandir，大小/时间用 bulk。
- `windows.py` 提供 A：GetFileInformationByHandleEx + FileFullDirectoryInfo（256 KiB）；B：FindFirstFileExW + FindExInfoBasic + LARGE_FETCH。Windows API 返回固定结构，多余字段不在业务层使用。reparse point 不进入。未有 Windows SMB 基准，自动模式继续使用 CPython scandir，A/B 可在界面或基准显式选择。
- API 不支持、返回结构缺失等能力问题，会丢弃该目录尚未交付的快照，重新用 scandir 枚举。访问拒绝、断网、超时照常交给原有失败/网络重试，不被 fallback 吞掉。没有对已交付的半目录直接拼接 fallback，避免重复文件。

目录并发与复制并发独立。扫描作业最多 scanWorkers 个，待处理目录用临时磁盘队列；主调度线程消费目录快照，复制使用原有上限 2×parallelism 的批次。大目录快照溢写、失败摘要限长、忽略路径与删除演练集合溢写，避免积累整个文件树的 Python Path 对象。复制保留临时文件、fsync、长度和源变化检测、时间保留、原子替换、限速、日志、暂停及网络恢复。

扫描后续目录时前面目录可复制；同一个超大目录仍要先完成本目录枚举。网络调用阻塞只能等系统返回，不能保证立即暂停。任务关闭会释放临时缓冲；强制杀进程后系统临时目录可能留有一次性缓冲，不会自动复用。

## 指标口径

实时 API 的 progress.scanMetrics 和完成报告 mappings[].scanMetrics 包含源/目标条目、目录数、显式 metadataCalls/extraStatCount、comparisonExistsCalls、nativeCalls、fallbacks、目录队列长度及复制队列上界估计。界面提供两侧条目速率、目录速率、复制吞吐与原有 copy/skip/delete 日志计数。

这些是 Python/原生 API 计数，不是 SMB/NFS 报文数。nativeCalls 是一次批量 API 调用，不保证只有一个网络包；metadataCalls 不含内核隐式类型查询、根/路径安全复核、写入校验、utime/chmod。速率为自启动均值，原有复制速率仍为短窗口。复制队列值包括运行中的批次，按最大批量估计，不冒充精确文件数。

## 基准及复现

`scripts/benchmark_directory_pipeline.py` 只操作新建临时目录，输出 JSON。默认演练；`--copy` 才实际复制。样本已存在且属性相同，SKIP/COMPARE 主要测枚举与比较，不能当作复制吞吐。日志回调为空，未计 API 日志入库成本。CPU 为进程 CPU 差值，RSS 为整个进程峰值（含准备阶段），网络包数为空，需外部抓包。

```bash
# 10 万单目录、旧路径对比（AUTO 为保留的旧引擎）
python scripts/benchmark_directory_pipeline.py --files 100000 --directories 1 --workers 1 --strategies AUTO,COPY_ALL,SKIP_EXISTING,COMPARE_METADATA --output flat.json
# 目录并发矩阵，显式比较 macOS native 与 portable
python scripts/benchmark_directory_pipeline.py --files 100000 --directories 1000 --workers 1,4,8,16,32 --backends portable,macos --output tree.json
# 百万文件，可将数量继续放大；每侧都会创建该数量的样本文件
python scripts/benchmark_directory_pipeline.py --files 1000000 --directories 10000 --bytes 0 --workers 1 --output million.json
# Windows A/B 与 scandir，请在 Windows CMD 内运行
python scripts/benchmark_directory_pipeline.py --files 100000 --backends portable,windows_bulk,windows_find --output windows.json
# 实际复制 1000 个 4 KiB 文件
python scripts/benchmark_directory_pipeline.py --files 1000 --directories 10 --bytes 4096 --workers 1 --backends auto --copy --output copy.json
```

A 本地→本地使用默认临时目录；B 加 `--target-parent /挂载共享`；C 加 `--source-parent /挂载共享`；D 两项分别指定共享目录。Windows 可传 `--target-parent "\\server\share"`。请选择有权限的新样本父目录，脚本不遍历或修改父目录已有内容。

实测结果和环境限制见下方最终记录。

## 未解决或未验证

- 当前机器无 SMB/NFS 挂载；B/C/D 与真实网络请求数未实测，不推断网络加速倍数。
- Windows 原生 A/B 的 SMB 效果、Linux NFS 效果须对应环境执行基准；不能根据 macOS 宣称三平台速度已验证。
- 本次不实现数千万规模全量实测、实时监视、持久索引、Rust 扩展、内存占用比例自动调整。
- 旧 AUTO/双向仍保留原扫描路径及全树计划，切换策略是显式动作。
- 服务端大小写与 Unicode 规则未知时，疑似等价但不完全同名会安全报错，建议统一名称或使用 AUTO；没有按客户端平台猜测。
- COPY_ALL 不比较目标，但实际写入前的目标根/链接检查仍然存在。其总体复制耗时可能主要花在安全、写入、fsync、时间校验；本次不以牺牲这些机制换速度。

## 依据

- [Apple XNU attr.h](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/attr.h)：Darwin 属性常量与结构。
- [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)、[FILE_FULL_DIR_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_full_dir_info)、[FindFirstFileExW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-findfirstfileexw)：Windows 目录信息接口。
- [Linux getdents](https://man7.org/linux/man-pages/man2/getdents.2.html)：目录批量枚举及 DT_UNKNOWN 限制。

## 最终测量记录

环境：本机 macOS、Python 3.12、本地文件系统。样本为独立临时目录，完成后已清理；没有操作用户同步目录。以下为单轮热缓存测量，不是跨平台或网络性能保证。

### 10 万文件 / 1000 子目录：最终 auto 并发矩阵

| 目录 worker | SKIP_EXISTING 秒 | COMPARE_METADATA 秒 |
|---|---:|---:|
| 1 | 1.527 | 2.162 |
| 4 | 1.850 | 1.891 |
| 8 | 1.930 | 1.900 |
| 16 | 1.918 | 1.914 |
| 32 | 1.898 | 1.981 |

保守默认 1：本地并发收益有限，不能外推到高 RTT 共享；允许用户独立调节扫描与复制并发。

### 其他规模

| 样本 | 策略 | 秒 | 文件/秒 | 进程峰值 MiB |
|---|---|---:|---:|---:|
| 100k-flat | AUTO | 24.069 | 4155 | 50.9 |
| 100k-flat | COPY_ALL | 21.586 | 4633 | 50.9 |
| 100k-flat | SKIP_EXISTING | 3.891 | 25698 | 50.9 |
| 100k-flat | COMPARE_METADATA | 3.886 | 25736 | 52.1 |
| 1m-tree | SKIP_EXISTING | 47.631 | 20995 | 42.3 |
| 1m-tree | COMPARE_METADATA | 52.752 | 18957 | 44.5 |
| copy-1000 | COPY_ALL | 0.488 | 2049 | 26.0 |
| copy-1000 | SKIP_EXISTING | 0.016 | 62459 | 26.0 |
| copy-1000 | COMPARE_METADATA | 0.024 | 41610 | 28.8 |

10 万平面目录相同文件比较：保留的旧 AUTO 路径约 24.1 秒，目录 metadata 路径约 3.89 秒，约 6.2 倍；该轮有后台样本准备干扰，不能作为稳定倍数承诺。COPY_ALL 仍受每文件实际写入安全步骤影响，即使演练也保留路径检查，不能套用相等文件跳过的提速倍数。

百万文件 + 1 万目录：metadata 两侧合计枚举 202 万条目，40,008 次批量枚举 API，显式逐条 metadata API 为 0；完整比较约 52.75 秒，峰值约 44.5 MiB。真实 1000×4 KiB COPY_ALL：目标扫描条目为 0、复制 1000 个，约 8.39 MB/s（包括安全复核、fsync 和时间写入；小文件不是顺序大文件吞吐）。

原始记录：[最终并发矩阵](docs/benchmarks/100k-tree-final.json)、[原生/portable 比较](docs/benchmarks/100k-tree-native-comparison.json)、[平面目录](docs/benchmarks/100k-flat.json)、[百万样本](docs/benchmarks/1m-tree.json)、[真实复制](docs/benchmarks/copy-1000.json)。

### 验证中的运行库问题

一次回归进程卡在 SQLite 3.51.1 的 unixOpen/findReusableFd 与 sqlite3WalClose/unixLock 锁等待。调用栈与 [SQLite 官方 3.51.2 发布说明](https://www.sqlite.org/releaselog/3_51_2.html)描述的 Unix 锁序死锁相符（原因推断）。API 对 3.51.0/3.51.1 的短数据库事务增加串行保护，其他版本保持原并发，不修改用户数据库内容或升级依赖。之后完整回归通过。

### 审查修复

- SKIP_EXISTING 发布使用系统原子“不覆盖”操作；枚举后出现同名目标也会保留，记录跳过。不支持原子排他改名时尝试同目录硬链接；文件系统两者均不支持则报错，绝不改成覆盖。
- 源端同一目录大小写/Unicode 等价名称在生成操作前拒绝；目录名称也做源目标等价检查，避免跨平台合并到同一路径。
- Windows 原生错误保留 WinError→errno 的系统分类，路径不存在可正确进入空目标目录逻辑；新增子目录有专项测试。
- 大小时间跳过依据本次目录快照，不是文件系统一致性快照；外部程序并发修改树时不存在跨所有文件的原子视图。实际复制仍检查源文件身份、大小和时间变化。

### 本地验证完成情况

- 141 项 Python 单元/API 回归通过，包括原有功能、扫描回退、溢写、原子不覆盖竞态、名称冲突、权限/网络恢复、暂停和追加目录。
- Playwright 真实浏览器流程通过，包含策略切换、预设恢复、原有任务/目录/日志/报告及暂停恢复。
- 0.1.7 zip/tar 内容一致且校验和有效，无用户数据库或虚拟环境；解压后隔离启动及 22 项目录策略测试通过。
- 独立代码审查发现的 3 项重要问题已修复并复核；GitHub 三平台 CI 结果以对应提交的 Checks 为准，不以本机结果替代。

Windows CI 首轮捕获隔离 Python 忽略 PYTHONUTF8 后，cp1252 管道输出中文失败；入口显式设置 UTF-8，新增非 Unicode 管道回归复现及验证。该问题属于入口输出编码，不是扫描后端失败。
