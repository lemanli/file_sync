"""从当前工程白名单打包，统一 Windows 启动文件编码和换行。"""
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build():
    version = (ROOT / 'VERSION').read_text().strip()
    prefix = f'file_sync-v{version}'
    # 仅打包受版本管理的文件及本次新增发布文件，避免携带数据、环境或凭据。
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    names = sorted(set(filter(None, tracked)) | {'scripts/build_dist.py', 'docs/RELEASE-0.1.1.md', 'docs/RELEASE-0.1.2.md', 'docs/RELEASE-0.1.3.md', 'backend/standalone/control.py', 'scripts/migrate_data.py', 'test/test_task_control.py', 'docs/plans/2026-09-22-task-control.md', 'docs/RELEASE-0.1.4.md', 'backend/standalone/storage.py', 'test/test_storage.py', 'migrate-data.cmd', 'docs/RELEASE-0.1.5.md', 'backend/standalone/scanning.py', 'backend/standalone/recovery.py', 'scripts/benchmark_scan.py', 'test/test_scanning.py', 'test/test_network_recovery.py', f'docs/RELEASE-{version}.md'})
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    paths = [dist / f'{prefix}.zip', dist / f'{prefix}.tar.gz']
    with zipfile.ZipFile(paths[0], 'w', zipfile.ZIP_DEFLATED) as zipped, tarfile.open(paths[1], 'w:gz') as tar:
        for name in names:
            path = ROOT / name
            data = path.read_bytes()
            if path.suffix in {'.ps1', '.cmd'}:
                data = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
                if path.suffix == '.ps1':
                    data = b'\xef\xbb\xbf' + data
            archive_name = f'{prefix}/{name}'
            zipped.writestr(archive_name, data)
            info = tarfile.TarInfo(archive_name)
            info.size = len(data)
            info.mode = 0o755 if path.suffix == '.sh' else 0o644
            tar.addfile(info, io.BytesIO(data))
    sums = ''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in paths)
    (dist / 'SHA256SUMS').write_text(sums, encoding='ascii')
    print(sums, end='')


if __name__ == '__main__':
    build()
