# 0.1.14：定位剩余的目标路径检查开销

## 依据与范围

第 18 次运行（0.1.13）相比第 16 次（0.1.12），平均复制速率从 18.6 增至 28.6 文件/秒、平均写入吞吐从 3.22 增至 6.04 MiB/s；路径检查平均耗时减半。但两项路径检查仍占累计线程时间的 68.5%，近期窗口约 73.7%。每次路径检查固定发生 7 次轻量查询、1 次回退、2 次 lstat（含根目录身份）。尚无证据证明回退必然来自共享根，需要细化测量。

本版只增加诊断，不减少检查或修改复制行为，不承诺直接提速。没有修改线程上限、同步策略、数据库结构和配置协议（仍为 5）。

## 开启与导出

沿用任务“性能诊断日志”开关 debugPerformance。打开后运行 5–10 分钟，在执行记录的日志页点击“导出性能诊断”。已开启的任务无需重新设置。暂停时可以修改开关；详细统计仅覆盖开启期间，关闭后保留已有累计数据，不再计入新的详细查询。

以下字段出现在 progress 和周期 performance_debug 诊断中：

- pathCheckMetrics：保留原有计数，增加固定分类的耗时与次数。
- attributesAncestorSeconds / Count：普通路径轻量查询，包括文件路径和非共享根的父目录。
- attributesShareRootSeconds / Count：UNC 共享根的轻量查询。
- lstatAncestorSeconds / Count、lstatShareRootSeconds / Count：完整查询回退，按路径类别区分。
- identityTargetRootSeconds / Count：目标根目录身份核验；Windows 为合并链接判断的 lstat，其他平台为 stat。
- fallbackReparse：重解析点触发回退；fallbackNotFound、fallbackPathNotFound、fallbackAccessDenied、fallbackBadNetworkPath、fallbackBadNetworkName、fallbackInvalidName、fallbackLongPath、fallbackOtherError：固定的原生失败分类，不为每个错误码创建动态计数字段。
- pathCheckSamples：路径、查询类型、路径类别、耗时、原因；原生失败含 winError。elapsedSeconds 是该样本汇总时的任务运行秒数，用来判断新旧。

Seconds 为累计线程秒数，不是墙钟耗时；以同类别 Seconds/Count 计算平均值。共享根身份若也涉及别的查询，其计数不能和旧的 rootIdentityQueries 再相加。指标仅覆盖目标安全检查，不表示整个进程的所有 metadata 请求，更不表示实际 SMB 网络包数。

## 有界与安全

- 原生失败后立即读取线程局部 Windows 错误码，再进行后续计时和 lstat，避免丢失失败原因。
- 单轮路径检查最多保留 4 条候选；全任务每 10 秒最多接受 8 条，内存缓冲最多 8 条。返回最近保留的样本，重复导出不清空；它们不是完整事件清单或统计学分位数。
- 查询达到 50 ms 才进入慢查询候选；失败与重解析点回退不受此阈值限制。先到样本可能占满窗口，应以累计分类耗时判断整体占比。
- 关闭诊断时不进行逐查询详细计时或保存路径样本，基本计数保留。
- 不新增逐路径日志和数据库事务；使用原有每约 10 秒汇总和导出流程。详细诊断增加少量计时和字典操作，不声称零开销。
- 无跨文件安全缓存；访问失败仍按原逻辑传播，网络重试、安全终止与删除保护不变。
- 样本含真实路径，随本机诊断文件导出，不会自动上传。

## 验证与升级

新增测试覆盖：共享根错误码及回退耗时、关闭详细诊断不读取计时器、异常传播仍计时、两层抽样上限、任务开关集成和固定错误分类。Windows CI 纳入新测试。

正常停止旧进程，解压 0.1.14 并沿用原数据库目录和 Python 配置后启动。无需迁移数据库；仍不承诺跨进程保留扫描游标。
