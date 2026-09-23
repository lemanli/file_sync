# File Sync 0.1.5

直接关闭终端留下的未结束记录不再阻止迁移。副本中的任务执行标记为中断并保留日志，原库不改写；不是从进程内扫描位置恢复。

在 env/standalone/application.yml 中设置 database.directory 固定数据目录；可选 database.migrate_from 设置旧目录或数据库文件。启动时自动迁移一次，已有目标库则直接复用。双击 migrate-data.cmd 可只准备数据，双击 start-standalone.cmd 可准备并启动。

新版启动和迁移采用本机进程锁；异常退出自动释放锁，不需要删 .lock 文件。旧版不支持这把锁，首次切换仍需退出旧程序。本机多版本轮流共享目录，不支持多个机器共用网络 SQLite 数据库。

包含旧任务、历史、日志迁移测试和锁进程强制退出测试。内存预算及扫描加速未包含在本版。
