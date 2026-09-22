"""生成可审计的比较结果；时间均使用绝对时间戳，不按操作系统调整时区。"""

def method_name(value):
    if isinstance(value, dict):
        return value.get('comparisonMode') or ('sha256' if value.get('checksum') else 'size_mtime')
    if isinstance(value, str):
        return value
    return 'sha256' if value else 'size_mtime'


def tolerance_ns(config):
    # 旧引擎调用未传新字段时仍按原来的精确时间比较。
    return round(float(config.get('timeToleranceSeconds', 0)) * 1_000_000_000)


def metadata(size, mtime_ns):
    # 纳秒用字符串存储，避免浏览器 Number 损失精度。
    return dict(size=size, mtimeNs=str(mtime_ns))


def compare(source, target, left, right, mode, digest, tolerance=0):
    mode = method_name(mode)
    detail = dict(method=mode, source=left, target=right, timeToleranceSeconds=tolerance / 1_000_000_000)
    if right is None:
        return False, dict(detail, reason='目标不存在')
    if left['size'] != right['size']:
        return False, dict(detail, reason='文件大小不同')
    if mode == 'size':
        return True, dict(detail, reason='文件大小相同；未比较内容和时间')
    if mode == 'sha256':
        left['sha256'], right['sha256'] = digest(source).hex(), digest(target).hex()
        equal = left['sha256'] == right['sha256']
        return equal, dict(detail, reason='SHA-256 相同' if equal else 'SHA-256 不同')
    difference = abs(int(left['mtimeNs']) - int(right['mtimeNs']))
    equal = difference <= tolerance
    return equal, dict(detail, differenceNs=str(difference), reason='大小相同且修改时间差在容差内' if equal else '修改时间差超过容差')
