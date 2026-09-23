# File Sync 0.1.2

修复目录安装 Python 的模块搜索路径：入口显式加载 backend，支持隔离模式启动。包含 0.1.1 的 CMD 直接启动、自定义 Python/direct 模式及网络共享同步修复。

Windows 解压新包，在 env/install.ini 设置 python = E:\python\python.exe 和 environment = direct，然后运行 start-standalone.cmd。pip 更新通知不影响启动，无需为此升级 pip。

macOS 如仍运行旧 mng_sync 工程，应停止旧进程后从希望使用的工程启动，确保加载对应修复。切换工程前备份并迁移自己的配置和任务数据；不要同时运行两个副本同步同一目标。
