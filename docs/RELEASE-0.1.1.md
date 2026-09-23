# File Sync 0.1.1

- Windows 双击入口直接使用 CMD 调用 Python，无需 PowerShell；支持配置解释器和透传安装参数。
- 修复 Windows PowerShell 中文脚本的编码兼容性，启动脚本统一读取 Python 配置。
- 支持独立 Python 目录与 direct/venv 模式；例如在 env/install.ini 设置 python = E:\python\python.exe、environment = direct。
- 修复 macOS 文件同步至网络共享时复制 BSD 标志导致的 Errno 22。
- 新包包含以上修复。升级前停止旧程序，备份数据与配置，解压到新目录后按需迁移。
