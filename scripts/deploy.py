"""单机版部署：自动检查环境和依赖，启动本机 SQLite 文件同步。"""
from pathlib import Path
import argparse
import configparser
from urllib.parse import urlsplit
from urllib.request import urlopen
import hashlib
import json
import os
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]

PACKAGE_SOURCES = {
    'pypi': 'https://pypi.org/simple',
    'tuna': 'https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple',
}


def source_setting(root, override=None):
    """安装源配置用标准库读取，首次启动不依赖 PyYAML。"""
    if override:
        return override.strip()
    if os.environ.get('FILE_SYNC_PIP_SOURCE'):
        return os.environ['FILE_SYNC_PIP_SOURCE'].strip()
    settings = configparser.ConfigParser(interpolation=None)
    path = root / 'env/install.ini'
    if path.exists():
        with path.open(encoding='utf-8-sig') as stream:
            settings.read_file(stream)
    return settings.get('pip', 'source', fallback='auto').strip()


def pip_has_source(python):
    # 保留企业源、离线安装及代理/证书配置，不擅自改成公共源。
    keys = ('PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_NO_INDEX', 'PIP_FIND_LINKS',
            'PIP_PROXY', 'PIP_CERT', 'PIP_CLIENT_CERT')
    if any(os.environ.get(key) for key in keys):
        return True
    result = subprocess.run([str(python), '-m', 'pip', 'config', 'list'],
                            capture_output=True, text=True, check=True)
    options = {'index-url','extra-index-url','no-index','find-links','proxy','cert','client-cert'}
    return any(line.split('=',1)[0].rsplit('.',1)[-1].strip() in options
               for line in result.stdout.splitlines() if '=' in line)


def reachable_source(url):
    try:
        # 探测包索引而不是主页；不下载完整包，不关闭 HTTPS 校验。
        with urlopen(url.rstrip('/') + '/fastapi/', timeout=3) as response:
            return response.status == 200 and b'fastapi' in response.read(4096).lower()
    except Exception:
        return False


def installation_sources(python, setting):
    if setting == 'configured' or (setting == 'auto' and pip_has_source(python)):
        print('依赖安装：沿用本机 pip 源/离线/代理配置，不自动切换。', flush=True)
        return [None]
    if setting == 'auto':
        available = [url for url in PACKAGE_SOURCES.values() if reachable_source(url)]
        if not available:
            raise RuntimeError('官方源和清华镜像均未通过连通检测，请检查网络，或在 env/install.ini 指定 source=tuna / configured / 镜像HTTPS地址')
        print('自动检测通过，将优先使用：' + available[0], flush=True)
        return available
    url = PACKAGE_SOURCES.get(setting, setting)
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('安装源请选择 auto、configured、pypi、tuna 或不含凭据的 HTTPS 索引地址；认证源请使用本机 pip 配置')
    print('依赖安装主索引：' + url, flush=True)
    return [url]


def install_requirements(python, requirements, sources, force=False):
    for index, source in enumerate(sources):
        args = [str(python), '-m', 'pip', 'install', '--timeout', '15', '--retries', '1']
        if source:
            args += ['--index-url', source]
        if force:
            args.append('--force-reinstall')
        args += ['-r', str(requirements)]
        try:
            subprocess.run(args, check=True)
            # 修复安装继续使用刚刚成功的源，不重新探测。
            return [source]
        except subprocess.CalledProcessError:
            if index + 1 == len(sources):
                raise
            print('安装未成功，尝试下一个已通过检测的源：' + sources[index+1], flush=True)


def interpreter_ready(python):
    if not python.exists():
        return False
    try:
        return subprocess.run([str(python), '-c', 'import sys,venv; assert sys.version_info >= (3,10)'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def standalone_ready(python, requirements):
    """检测实际可用依赖，不能只凭虚拟环境目录存在就跳过安装。"""
    if not python.exists():
        return False
    pins = [line.strip().split('==', 1) for line in requirements.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    code = ("import sys,json,importlib.metadata as m; "
            "assert sys.version_info >= (3,10); "
            "import fastapi,uvicorn,yaml; "
            "assert all(m.version(name)==version for name,version in json.loads(sys.argv[1]))")
    try:
        result = subprocess.run([str(python), '-c', code, json.dumps(pins)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            return False
        return subprocess.run([str(python), '-m', 'pip', 'check'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def prepare_standalone(root, package_source=None):
    """首次准备，后续复用；依赖损坏或清单更新时才重新安装。"""
    requirements = root / 'backend/requirements-standalone.txt'
    for relative in ('backend/standalone_app.py', 'backend/standalone/index.html',
                     'env/standalone/application.yml', 'backend/requirements-standalone.txt'):
        if not (root / relative).is_file():
            raise RuntimeError('工程文件缺失：' + str(root / relative) + '；请完整解压工程后启动')
    env_dir = root / '.venv-standalone'
    python = env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not interpreter_ready(python):
        if env_dir.exists():
            # 跨系统复制或不完整的环境先保留，避免覆盖其中未知内容。
            import uuid
            backup = root / ('.venv-standalone.backup-' + uuid.uuid4().hex[:8])
            env_dir.rename(backup)
            print('旧环境不适用于本机，已保留到：' + str(backup), flush=True)
        print('首次启动：正在创建 Python 环境…', flush=True)
        venv.EnvBuilder(with_pip=True).create(env_dir)
    marker = env_dir / '.sync-requirements.sha256'
    signature = hashlib.sha256(requirements.read_bytes()).hexdigest()
    changed = marker.exists() and marker.read_text(encoding='utf-8').strip() != signature
    if changed or not standalone_ready(python, requirements):
        print('正在安装或修复依赖，准备检查安装源…', flush=True)
        subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True)
        sources = installation_sources(python, source_setting(root, package_source))
        sources = install_requirements(python, requirements, sources)
        if not standalone_ready(python, requirements):
            # 版本元数据正常但模块文件缺失时，普通 pip install 可能不会重新下载。
            install_requirements(python, requirements, sources, force=True)
        if not standalone_ready(python, requirements):
            raise RuntimeError('依赖检查仍未通过，请查看上方安装错误')
    else:
        print('环境和依赖检查通过，直接启动，无需重新安装。', flush=True)
    marker.write_text(signature, encoding='utf-8')
    return python


def main():
    parser = argparse.ArgumentParser(description='文件同步单机版：自动准备环境并启动')
    # 保留 standalone 参数兼容既有快捷方式；不再提供服务器部署入口。
    parser.add_argument('mode', nargs='?', default='standalone', choices=['standalone'])
    parser.add_argument('--pip-source', help='安装源：auto、configured、pypi、tuna 或 HTTPS 索引地址')
    parser.add_argument('--auto', action='store_true', help='兼容旧命令；默认即自动检查环境')
    parser.add_argument('--skip-install', action='store_true', help='直接使用已有虚拟环境，不检测或修复依赖')
    parser.add_argument('--prepare-only', action='store_true', help='只准备单机环境，不启动')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('请使用 Python 3.10 或更新版本，建议 Python 3.12')
    if args.auto and args.skip_install:
        parser.error('--auto 不能与 --skip-install 同用')
    if args.skip_install:
        python = ROOT / '.venv-standalone' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.exists():
            parser.error('单机虚拟环境不存在，请先使用默认自动部署')
    else:
        python = prepare_standalone(ROOT, args.pip_source)
    if args.prepare_only:
        print('单机环境准备完成；下次直接双击 start-standalone.cmd 或运行本脚本即可')
        return
    entry = ROOT / 'backend/standalone_app.py'
    print('启动单机版：' + str(entry), flush=True)
    try:
        subprocess.run([str(python), str(entry)], cwd=ROOT, check=True)
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
