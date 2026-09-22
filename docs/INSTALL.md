# 安装与升级

## Windows 首次安装

1. 安装Python 3.10+，推荐本次验证使用的3.12，启用Launcher或PATH。
2. 从GitHub Release下载zip，完整解压到可写目录，如 D:/file_sync。不要在压缩包内直接启动。
3. 双击 start-standalone.cmd。依赖首次安装通常需要联网，已有pip离线配置也可沿用。
4. 浏览器访问 http://127.0.0.1:9098。启动失败窗口会保留错误。

脚本按自身位置定位工程，支持空格目录，不用手工切换工作目录。正常运行时保留终端窗口。

## 镜像及代理

env/install.ini 的 source 支持 auto、tuna、pypi、configured 或HTTPS索引地址。优先级：--pip-source > FILE_SYNC_PIP_SOURCE > 配置文件 > auto。

自动模式保留本机pip源、离线、代理和证书设置。无配置时探测官方与清华，安装失败有限尝试备用源；显式源失败不擅自切换。脚本不改系统pip配置、不关闭证书校验。额外索引与no-index等本机策略仍由pip处理。

参考：[pip配置](https://pip.pypa.io/en/stable/topics/configuration/)、[清华PyPI镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)。

## macOS / Linux

运行 bash scripts/deploy.sh；或设置 SYNC_PYTHON 指定Python解释器。已有单机虚拟环境优先使用。

## 更新与备份

先停止程序，备份完整 data/standalone 目录及自己修改的配置。将新版本解压到新目录，复制配置和数据后启动，检查历史及任务路径。不要迁移旧 .venv-standalone，脚本会创建对应平台的新环境。数据在首次启动时自动建表，不需要导入SQL。

本机仅允许一个进程打开同一份任务数据库。不要同时启动两个副本同步同一目标。

## 故障定位

- Python找不到：重新安装并开启Launcher/PATH。
- 网络失败：改source=tuna，或配置本机pip代理后选configured。
- 端口占用：确认没有重复运行，或修改application.yml端口。
- 共享目录拒绝访问：以启动程序的同一账户在资源管理器验证共享权限。
- 写时间失败：检查共享协议/文件系统权限与时间精度，调整时间容差；保留时间失败不会替换已有目标。
