# 0.1.17：NAS 兼容根目录身份快查

## 与 0.1.16 的区别

0.1.16 只停止反复失败的原生探测，回到逐次 Python lstat。本版增加实际可执行的兼容快查：扩展原生接口回退后，尝试 GetFileInformationByHandle；仅在它与当前 Python 完整查询结果一致时启用。目录身份没有缓存，每次仍重新打开当前路径。

本轮未收到 0.1.16 新诊断，无法断言用户 NAS 具体是哪一步回退。新增路径针对接口兼容性，不宣称已在该 NAS 上测得加速。若失败发生在目录打开而非扩展属性/身份接口，兼容路径也可能不可用，此时仍安全回到 0.1.16 的处理。

## 查询与选择

1. 每组首次只让一个线程探测，原生快查可用则保持原生。
2. 原生回退得到 Python lstat 结果后，尝试兼容接口读取目录属性和 64 位身份。
3. 完整 FileIdInfo 可用时仍读取并保留完整 128 位身份及卷标识；只有明确返回“不支持”等兼容性错误时，才在本组停止重复请求该接口。
4. 比较完整整数形式的卷标识、文件身份、类型和重解析标记。非零且完全一致才采用兼容路径。不截断 Python 的 128 位身份或卷标识来促成匹配。
5. 不能校准时不启用，重新读取完整身份交给原有安全判断；兼容接口不可用时本组不再反复探测。查询或网络故障仍传播。
6. 启用后普通目录在完整身份接口不支持的情况下，每次只需打开句柄、一次 GetFileInformationByHandle、关闭句柄。完整身份接口支持时额外保留一次 FileIdInfo 查询。
7. 重解析点仍查询并核验标记，不把兼容接口成功当作“不是链接”。非链接重解析点保留原文件系统专用行为。

沿用逐次安全检查、共享读取的短生命周期句柄、异常关闭、目录替换检测、时间保留、刷盘、删除保护。首次校准之外，兼容快查保持并行。状态只属于当前目录组，不跨任务持久化。

Microsoft 文档指出：GetFileInformationByHandle 支持 SMB，但可能返回不完整信息；ReFS 的 64 位文件编号不能保证唯一。因此本实现拒绝零身份，保留可用的完整身份，并用原查询校准，而非直接用 64 位编号替代所有身份。

## 指标与实测方法

- rootLegacySelected：兼容路径校准通过并启用的次数，通常每组一次。
- rootLegacyQueries：兼容路径查询次数，启用后随核验次数增长。
- rootLegacyMismatch：身份格式、值或类型未匹配，没有启用该路径。
- rootLegacyUnavailable、rootLegacyFailure*、rootLegacyError*：兼容路径不能使用的原因。
- rootCompatId128Queries：兼容路径中完整身份查询次数。
- rootCompatId128Unsupported / rootCompatId128Error*：完整身份接口不支持，本组后续不再重复探测。
- rootNativeBypassed：当前仍使用纯 Python 兼容路径的次数。
- identityTargetRootSeconds / Count：仍是整次根目录身份核验耗时，作为实际效果判断依据。

升级后保持性能诊断开启，运行 5–10 分钟导出。先确认 rootLegacySelected 是否出现、rootLegacyQueries 是否增长，再比较根核验平均耗时和同段时间 copied 增量。若未选中兼容路径，不能把版本升级当作性能优化成功。

只读基准已增加 adaptive_identity，比较 Python、旧原生和自动选择后的路径：

```cmd
E:\python\python.exe scripts\benchmark_root_identity.py "\\NAS\share\目标根目录" --checks 320 --workers 1 8 32 --rounds 2 > root-benchmark.json
```

不扫描、不创建或删除目录内容。交替顺序降低缓存偏差，每次身份必须匹配基准，否则停止。基准包含首次选择成本，不能代替实际同步吞吐测试。

## 验证结果与限制

本机 201 项回归：197 项通过，4 项原生 Windows 测试跳过；隔离应用启动通过。新增覆盖兼容接口 ABI（52 字节结构）、网络异常关闭、重解析标记、128 位身份不截断、零身份拒绝、校准不匹配、运行中目录替换、接口失效后不重试、并行执行和原生 Windows 兼容行为。Windows CI 纳入新测试。

可重复的模拟回归：原生接口不可用、兼容接口结果一致时，100 次身份核验仅首次执行一次原 lstat，兼容接口实际执行 100 次；未减少核验次数或缓存结果。这个结论是查询路径验证，不是 NAS 吞吐基准，也不证明真实环境一定加速。

## 升级

0.1.17，配置协议 5，无数据库迁移。正常停止旧程序，解压新包并沿用数据库目录和 Python 配置。任务定义保留，不保证跨进程保存扫描游标。

## 依据

- [GetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle)：接口和 SMB 支持边界。
- [BY_HANDLE_FILE_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information)：身份字段及 ReFS 限制。
