# 0.1.15：减少 Windows 根目录身份核验成本

## 诊断结论

第 19 次运行已明确：46840 次目标根目录身份核验累计约 17243 线程秒，每次约 368 ms，占全部累计线程时间约 63%。普通路径属性查询约 4.30 ms，共享根查询约 1.05 ms。固定回退是目标文件尚不存在（Windows 错误码 2），不是共享根故障。

核查 CPython 3.12.10 源码后发现，其 Windows lstat 慢路径会以 share mode 0 打开目录，且查询类型、重解析标记、完整文件信息、时间和身份。实际是否命中慢路径、独占打开竞争占多少时间仍需 Windows NAS 实测；不能将推断当作已测定的根因。

## 采用的方案

没有采用跨文件缓存或持久目录句柄。持久句柄需要额外处理祖先目录替换、SMB 断线、DFS 路径变化和重新连接。先使用更小的改动：每次核验都重新按当前路径打开目录，在这一短生命周期句柄上读取必要属性，然后立即关闭。

Windows/Python 3.12+：

1. CreateFileW 以 FILE_READ_ATTRIBUTES 打开目录。FILE_SHARE_READ | FILE_SHARE_WRITE 允许多个核验并行，不授予其他调用删除共享；句柄存续期间禁止删除/重命名该目录。
2. 使用 FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT，不跟随根目录链接。
3. GetFileInformationByHandleEx 的 FileAttributeTagInfo 获取类型与链接标记；FileIdInfo 获取 64 位卷身份和 128 位文件身份。不请求文件大小、时间、权限等无关字段。
4. 结果仍与任务开始时的根目录身份比较。目录替换、链接、断网、无权限和查询失败保留原安全/恢复处理流程。
5. 正常、异常、API 不支持等所有路径均关闭已取得的句柄。API 不支持或非链接重解析点回退原 lstat。原生错误不会伪造身份或缓存为成功。

旧 Python 不使用原生快查，以免与旧版本 inode 格式不一致；macOS/Linux 保持原有路径。初始根身份的 dev/ino 从同一次 stat 结果读取，避免两次重复查询。

复制前与发布前两次核验、路径链检查、原子不覆盖发布、文件时间、刷盘和删除保护均保留。句柄只保护本次查询期间，不宣称消除了原有检查与实际文件操作之间的全部竞态。

## 指标与测试

- 原有 identityTargetRootSeconds / Count 继续记录完整身份核验耗时，可直接与 0.1.14 对比。
- rootNativeQueries：尝试原生根目录查询的次数，不等于 Win32 或 SMB 调用总数。
- rootNativeFallbacks：回退原 lstat 的次数，包括旧 Python。
- lstatQueries 不再将原生快查算作 lstat，仅统计实际回退和其他完整查询。
- rootIdentityQueries 仍表示每次身份核验；不要和其子集计数相加。

本机 185 项回归：182 项通过，3 项 Windows 原生测试跳过。隔离启动冒烟通过。新增测试覆盖完整 128 位身份、共享模式、重新打开当前路径、链接标记、长路径、异常关闭、网络错误不静默回退、API 不支持回退、非链接重解析点语义、引擎两次调用。Windows CI 增加真实身份与 Python 对比、目录替换、32 线程查询和句柄释放检查。

当前环境没有 Windows SMB NAS，尚未取得实际优化后吞吐，不能宣称提速倍数。诊断路径耗时与同段时间 copied 增量才是实际验收依据。

提供只读 Windows 基准，不扫描或写入目录：

```cmd
E:\python\python.exe scripts\benchmark_root_identity.py "\\NAS\share\目标根目录" --checks 320 --workers 1 8 32 --rounds 2 > root-benchmark.json
```

交替测试 Python lstat 与原生快查，结果必须匹配当前基准身份，否则报错停止。每个 worker 一个查询循环，无海量 Future。建议任务停止时测试，减少同时运行任务对结果的干扰；操作系统缓存仍可能影响数据。

## 发布与升级

版本 0.1.15，配置协议仍为 5，无数据库结构迁移。停止旧程序，解压新包，沿用固定数据库目录和 Python 配置。任务定义保留；跨进程扫描游标不保证保留。

升级后保持性能诊断开启，运行 5–10 分钟再导出。重点查看根身份平均耗时是否下降、rootNativeFallbacks 是否异常偏高，以及失败数是否为零。

## 技术依据

- [CPython 3.12.10 posixmodule.c](https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c)：win32_xstat_slow_impl。
- [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)：共享模式、目录句柄与重解析标志。
- [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)：卷与完整文件身份。
- [FILE_ATTRIBUTE_TAG_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_attribute_tag_info)：类型和重解析标记。
