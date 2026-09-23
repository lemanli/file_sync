# 扫描架构分析（2026-09-23，修改前工作树）

## 调用链与成本

`api.execute → preflight_mapping（根目录读写探测） → synchronize → _synchronize_one`
依次执行 roots 校验、源/目标 scan_tree 全树预检、os.walk(source) 执行、copy_one 比较/复制、os.walk(target) 镜像删除。双向额外由 two_way 生成全树计划。

| 行为 | 当前情况 |
|---|---|
| os.walk | 执行源遍历、镜像删除、双向计划 |
| Path.exists / is_file / is_dir / stat / is_symlink | 存在，比较、目录与安全检查重复 |
| os.path.exists / isfile / getsize / getmtime | 主单向复制路径未直接调用；Path 属性查询具有同类成本 |
| DirEntry.stat | 预检仅类型；未知类型的 DirEntry 判断可能隐式 stat |
| 逐源文件查询目标 | exists → is_file → stat，存在目标至少 3 次属性 API |
| 每文件 Future | 小文件按 batchFiles 批次；大文件一个 Future；在途上限 2×parallelism |
| 完整预扫描后复制 | 是，源与目标均完整预检 |
| 全树 Path 集合 | 单向普通文件不保存全树，但忽略路径、失败日志和删除演练集合可增长；双向保留全树计划 |

设 S/T 是源/目标文件路径含自身的祖先链长度。普通已存在且相等的文件：源执行分派 stat 1 次、比较 stat 1 次、源路径安全 lstat S 次；目标 exists/is_file/stat 3 次、两遍安全 lstat 2T 次、目标根属性 2 次。即源约 2+S，目标约 5+2T 次 Python 属性 API（不含全树预检、目录级检查与根初始化）。未存在目标时比较部分只有 exists 1 次。

实际复制还包括源 open/fstat、复制后 stat、copymode 内部属性查询、源根身份复核；目标临时文件长度/时间读取、chmod/utime、fsync/replace、父目录安全复核。不能把这些与纯比较成本混为一谈，也不能把 Python 调用数当作真实网络包数；内核缓存、协议批处理决定实际 SMB/NFS 请求。

目前三平台统一 Python os.scandir/os.walk；Windows DirEntry 通常能复用枚举属性，POSIX 大小时间往往需要 stat。此前仅优化了预检类型读取，执行比较瓶颈仍存在。

## 决策

采纳策略分层、按目录比较、最小字段、平台扫描器、有限队列、可重复基准。新策略显式选择，`AUTO` 兼容原来的大小/hash/updateOnly/双向规则，避免升级后静默改变任务语义。COPY_ALL 不枚举目标；目标根及实际写入的安全检查保留，不作比较查询。SKIP_EXISTING 使用名称和类型。COMPARE_METADATA/MIRROR 固定大小和时间比较；不把旧的仅大小或 SHA 任务自动改成此策略。

目录内哈希表超过阈值落到本地临时 SQLite；目录工作队列也用临时 SQLite，避免超大平面目录和极宽树突破内存界限。这不是历史索引，任务退出即删除。按目录作业并发，复制仍沿用有界批次和临时文件发布。镜像删除保留最后阶段及失败禁删，优先保护安全，不承诺消除删除阶段全部属性检查。

保留 os.scandir；Linux 不重复实现 Python 已使用的 readdir，不在无证据时增加 ctypes syscall。Windows A/B 提供可选后端，无 Windows/SMB 实测不得宣称最优。macOS 原生后端本机验证。平台不支持时在完整目录快照层回退，避免中途回退重复或漏条目。访问拒绝/断网不伪装成“不支持”。

## 拟修改与验证

新增 scanner/{base,portable,linux,macos,windows}.py、directory_plan.py；修改 engine/api/progress 与现有表单，增加 scanner/pipeline 测试及基准脚本、结果报告、更新日志和 dist。

验证重点：覆盖、跳过、时间容差、Unicode、链接、超大目录溢写、原生回退、暂停/重试、失败禁删、旧任务兼容；API 比较查询计数独立于安全检查。基准记录本机真实结果，未提供的网络/Windows/Linux 环境明确未测。默认扫描并发保守选择，提供 1/4/8/16/32 参数矩阵，不根据单次本地测量推广网络结论。

## 风险

大小写/Unicode 名称等价、目录枚举后文件变化、Windows reparse point、网络错误与缺失目录混淆、ctypes 结构对齐、暂停时队列阻塞、临时磁盘不足均需显式处理。根和发布安全检查不能为了降低计数删除。COPY_ALL 无全目标预检，所以无关目标链接不再阻止任务，但写入路径上的链接仍拒绝。
