# 安装与升级

## Windows 首次安装

1. 准备Python 3.10+，推荐3.12。支持独立目录安装，不要求注册系统PATH。
2. 从GitHub Release下载zip，完整解压到可写目录，如 D:/file_sync。不要在压缩包内直接启动。
3. 双击 start-standalone.cmd。依赖首次安装通常需要联网，已有pip离线配置也可沿用。
4. 浏览器访问 http://127.0.0.1:9098。启动失败窗口会保留错误。

脚本按自身位置定位工程，支持空格目录，不用手工切换工作目录。正常运行时保留终端窗口。

## 选择 Python 与安装方式

双击入口直接在 CMD 控制台调用 Python，不依赖 PowerShell，并读取 `env/install.ini`。例如 Python 在 `E:\python`，希望直接安装依赖到其中：

```ini
[runtime]
python = E:\python\python.exe
environment = direct

[pip]
source = auto
```

`python` 也可填写 `E:\python`。随发布包携带 Python 时填写 `python = python`（工程下的 python 目录）。相对路径以工程目录为基准，路径有空格也无需加引号。留空时 Windows 优先检测随包 `python/python.exe`，然后检测 `py`、`python`。

- `direct`：使用指定 Python，依赖安装到该解释器的环境，不创建或要求 venv。
- `venv`：用指定 Python 创建工程下 `.venv-standalone`，保留原有默认行为。

pip 使用 `所选python.exe -m pip`，不需要把 `Scripts` 加入 PATH。已有 pip 时不调用 ensurepip；没有 pip 时尝试 ensurepip，若发行包两者均不提供，需要先为该解释器安装 pip。使用可导入标准库和第三方包的完整 Python；只解压官方嵌入式包不等于已配置好包搜索路径。

也可从 PowerShell 临时指定（优先于配置）：

```powershell
.\scripts\deploy.ps1 -PythonPath 'E:\python\python.exe' -Environment direct -PrepareOnly
.\scripts\deploy.ps1 -PythonPath 'E:\python\python.exe' -Environment direct
```

或直接使用 Python，不经过启动器：

```powershell
& 'E:\python\python.exe' .\scripts\deploy.py --environment direct --prepare-only
& 'E:\python\python.exe' .\scripts\deploy.py --environment direct
```

Python 入口同样支持 `--python`；环境变量为 `SYNC_PYTHON`、`FILE_SYNC_ENVIRONMENT`。优先级为命令行 > 环境变量 > 配置。`--skip-install` 复用所选模式，不安装依赖。后续双击继续使用保存的配置，并检测依赖后启动。

## 镜像及代理

env/install.ini 的 source 支持 auto、tuna、pypi、configured 或HTTPS索引地址。优先级：--pip-source > FILE_SYNC_PIP_SOURCE > 配置文件 > auto。

自动模式保留本机pip源、离线、代理和证书设置。无配置时探测官方与清华，安装失败有限尝试备用源；显式源失败不擅自切换。脚本不改系统pip配置、不关闭证书校验。额外索引与no-index等本机策略仍由pip处理。

参考：[pip配置](https://pip.pypa.io/en/stable/topics/configuration/)、[清华PyPI镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)。

## macOS / Linux

运行 bash scripts/deploy.sh；或设置 SYNC_PYTHON 指定Python解释器。安装模式同样由 install.ini 控制；默认使用 python3 引导安装。

## 更新与备份

先停止程序，备份完整 data/standalone 目录及自己修改的配置。将新版本解压到新目录，复制配置和数据后启动，检查历史及任务路径。不要迁移旧 .venv-standalone，脚本会创建对应平台的新环境。数据在首次启动时自动建表，不需要导入SQL。

本机仅允许一个进程打开同一份任务数据库。不要同时启动两个副本同步同一目标。

## 故障定位

- Python找不到：在 install.ini 配置实际 Python 路径，或设置 SYNC_PYTHON。
- 网络失败：改source=tuna，或配置本机pip代理后选configured。
- 端口占用：确认没有重复运行，或修改application.yml端口。
- 共享目录拒绝访问：以启动程序的同一账户在资源管理器验证共享权限。
- 写时间失败：检查共享协议/文件系统权限与时间精度，调整时间容差；保留时间失败不会替换已有目标。

## 重新打包

维护者在工程目录运行 `python scripts/build_dist.py`，使用当前工作区的受版本管理文件生成 `dist/file_sync-v版本.zip`、`.tar.gz` 和 `SHA256SUMS`。打包不会携带本地数据与虚拟环境；发布前应检查配置中没有个人路径或凭据。Windows PowerShell 脚本在包内统一为 UTF-8 BOM、CRLF。

CMD 下仅安装、不启动：`start-standalone.cmd --prepare-only`。临时选择直接安装：`start-standalone.cmd --environment direct`。PowerShell 脚本保留为可选入口。

## 固定数据目录、自动迁移与异常退出恢复

无需修改脚本。在 `env/standalone/application.yml` 设置固定本机目录：

```yaml
app:
  host: 127.0.0.1
  port: 9098
database:
  directory: 'E:/FileSyncData'
  migrate_from: 'E:/旧版/data/standalone'
```

- `directory`：固定的数据目录。程序使用其中的 `file_sync.sqlite3`，优先于旧配置 `path`。相对路径以工程为基准；跨版本共用时请使用绝对路径。
- `migrate_from`：可选，填写旧版数据目录或完整 sqlite3 路径。目标库不存在时自动备份迁移；存在时直接复用，绝不覆盖。
- 首次切换先退出旧程序，然后双击 `start-standalone.cmd` 自动准备数据并启动。只迁移、不启动，可双击 `migrate-data.cmd`。
- 新版本也填写同一个 `directory` 即可继续使用任务和日志，无需反复迁移。也可以直接将 `directory` 指向旧数据目录，此时不需要填写 `migrate_from`。
- 未设置 `directory` 时继续兼容 `database.path`；未设置迁移源时，新目录在首次启动时创建数据库。

关终端留下的 running/pausing/paused 记录，会在迁移副本或启动恢复时标为“已中断”，保留成功文件日志并记录恢复原因。迁移不改原库，也不恢复内存扫描位置，重新运行按比较规则处理已有文件。

新版用系统文件锁保护数据库。进程被关闭后锁自动释放，残留的 `.lock` 文件不代表程序仍在运行，不要删除它绕过锁。旧版没有这一保护，首次切换需确认旧 Python 进程已退出。

共用目录仅支持**同一台电脑上的本地磁盘，各版本轮流使用**。本程序的 SQLite WAL 模式不支持网络文件系统，不能放到 SMB/NAS 上供多台机器同时运行；同步源和目标仍可使用已挂载的网络目录。参考：[SQLite WAL 限制](https://sqlite.org/wal.html)。

数据目录必须位于任务同步范围之外；备份整个程序目录时建议固定到外部数据目录。跨机器迁移后，应检查任务中的绝对目录地址是否仍适用。

原命令仍兼容：

```cmd
E:\python\python.exe scripts\migrate_data.py --source "D:\旧版\data\standalone" --destination "E:\FileSyncData\file_sync.sqlite3"
```

macOS/Linux 可直接运行 `python scripts/migrate_data.py` 读取配置，或者运行 `bash scripts/deploy.sh --migrate-only` 自动准备依赖并读取配置。
