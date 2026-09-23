# File Sync · 单机文件同步

当前源码版本 **0.1.7**。通过本机网页管理本地目录和已挂载网络共享的同步任务，支持 Windows、macOS、Linux 的 Python 运行环境。

免登录、SQLite 存储、无需单独启动前端。无需 MySQL、Redis、RabbitMQ、Node.js、rsync 或 WSL。此仓库只提供单机程序，不包含多机调度服务器。

## 下载与启动

已发布包见 [Releases](https://github.com/lemanli/file_sync/releases)，请核对页面版本标签。获取当前最新源码：

    git clone https://github.com/lemanli/file_sync.git
    cd file_sync

先安装 Python 3.10+；当前主要使用 Python 3.12 验证。

### Windows

双击根目录 **start-standalone.cmd**。脚本自动寻找工程位置，首次创建环境并安装依赖；以后检查已有环境，正常时直接启动。

打开浏览器访问 **http://127.0.0.1:9098**。运行期间保持终端窗口开启，退出用 Ctrl+C。程序不会自行安装系统 Python，也不会注册系统服务。

### macOS / Linux

    bash scripts/deploy.sh

如默认 Python 版本过旧：

    SYNC_PYTHON=python3.12 bash scripts/deploy.sh

通用入口：

    python scripts/deploy.py

### 安装镜像

编辑 env/install.ini：

    [pip]
    source = auto

auto 优先沿用本机 pip 配置，否则检测官方源和清华镜像；tuna 固定清华主索引，pypi 固定官方，configured 使用本机配置。也可填写自定义 HTTPS 索引地址。环境已正常时不联网安装。

## 能做什么

- 最多100组源/目标目录；支持目录浏览、搜索和排序。
- 单向复制、双向合并、忽略规则、可选演练、重试及错误继续。
- 大小、大小＋修改时间、SHA-256三种比较；保留源修改时间及可调时间容差。
- 默认忽略源符号链接和特殊文件；目标目录预先进行读写检查。
- 普通文件先写临时文件，完成检查后替换；小文件批量并发，大文件流式复制。
- 实时阶段、文件进度、速率、计数；完成报告可联动日志筛选。
- 日志按状态、文件名/路径、目录组查询，显示总数、支持直接跳页。

新建任务默认仅比较大小、保留修改时间、2秒时间容差、不删除目标多余文件。**仅大小比较会跳过同大小但内容不同的文件**，需要识别此类变化时选择 SHA-256。

## 使用示例

源目录填写 C:/资料，目标填写 D:/备份；网络共享可填 UNC 路径（例如双反斜杠开头的 NAS 共享地址），也可使用当前账户可访问的映射盘。先在文件管理器中确认共享可读写，程序不保存网络共享密码。

新建任务 → 选择目录及规则 → 保存 → 运行。演练可选。报告入口位于任务列表“最近报告”及执行记录“查看报告”；点击复制、跳过、忽略、失败数量即可进入对应日志。

## 配置与数据

- env/standalone/application.yml：监听地址、端口及SQLite路径。
- data/standalone/file_sync.sqlite3：首次启动自动创建，存储任务与日志。
- env/install.ini：安装源选择。

数据库默认相对于工程根目录定位。程序只允许绑定本机回环地址。备份、更新或迁移前先停止程序，再复制完整 data/standalone 目录；不要边运行边只复制主数据库文件。升级保留自己的配置及数据，不复制其他电脑的虚拟环境。

## 目录结构

    backend/standalone/           同步引擎、API及静态界面
    backend/standalone_app.py     程序入口
    env/                         默认配置模板
    scripts/                     安装、启动与基准脚本
    test/                        隔离临时目录测试
    docs/                        安装、使用、发布说明
    start-standalone.cmd          Windows双击入口
    VERSION                      版本号

## 开发与验证

    python scripts/deploy.py --prepare-only
    .venv-standalone/bin/python -m pip install -r backend/requirements-standalone-test.txt
    .venv-standalone/bin/python -m unittest discover -s test -p 'test_*.py'

Windows 将解释器路径换成 .venv-standalone/Scripts/python.exe。部分路径竞争及符号链接测试依赖POSIX或创建链接权限，跨平台验证范围见发布说明。浏览器测试需要额外的 Node.js、Playwright 和 Chrome，仅开发测试使用，普通安装不需要。

## 0.1 已知边界

- 这是源码发行包，不是免Python的 exe 安装包。
- 尚未完成真实Windows设备和各种网络盘的完整验收。
- 单机同一时刻运行一个任务，不提供远程Agent或集中调度。
- 尚无取消按钮或大文件断点续传；退出需等待当前同步结束。
- 双向合并不传播删除；时间保留不承诺跨系统保留创建时间。
- 日志与扫描状态会随文件数增长，应预留数据库磁盘空间。
- 临时文件替换不等于断电情况下的完整事务保障。

详见 [安装说明](docs/INSTALL.md)、[使用说明](docs/USAGE.md)、[0.1.0发布说明](docs/RELEASE-0.1.0.md)。

## 许可证

保留本仓库原有 [MIT License](LICENSE)。

自定义 Python（如 `E:\python`）及免 venv 安装：在 `env/install.ini` 设置 `[runtime]` 的 `python` 路径和 `environment = direct`，详见 [安装说明](docs/INSTALL.md#选择-python-与安装方式)。

固定数据目录与自动迁移：在 `env/standalone/application.yml` 设置 `database.directory`，可选填写旧库目录 `database.migrate_from`，再双击启动；只准备数据可双击 `migrate-data.cmd`。本机多个版本轮流复用同一数据目录，关终端后的未结束记录自动标为中断。

## 0.1.7 扫描策略

已有任务默认保持 AUTO。普通单向同步可在编辑页选择“按目录比较大小和时间”，启用目录批量枚举和比较；只需覆盖复制选择 COPY_ALL，只补缺失文件选择 SKIP_EXISTING。镜像策略包含成功后安全删除。单向不建立持久索引。

详见 [当前扫描分析](CURRENT_SCAN_ANALYSIS.md)、[改造与基准结果](SCAN_OPTIMIZATION_RESULT.md)。Windows 原生 A/B 可选；自动模式默认 scandir，待真实 SMB 基准后再选择。
