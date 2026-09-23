"""仅识别连接、超时和存储 I/O 故障；权限和安全边界错误不自动重试。"""
import errno


class NetworkUnavailable(RuntimeError):
    pass


def network_error(error):
    codes={getattr(errno,name,-1) for name in ('ENETDOWN','ENETUNREACH','ECONNRESET','ECONNABORTED','ETIMEDOUT','EHOSTUNREACH','ENOTCONN','EPIPE','ESTALE','EIO')}
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error,(NetworkUnavailable,TimeoutError,ConnectionError)):
            return True
        if isinstance(error,OSError) and (error.errno in codes or getattr(error,'winerror',None) in {53,59,64,67,121,1231,1232,1236,1225}):
            return True
        error=error.__cause__ or error.__context__
    return False
