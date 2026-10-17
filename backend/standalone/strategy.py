"""仅选择执行路径，不修改已保存任务的同步语义。"""
from .comparison import method_name


def effective_strategy(config):
    strategy = config.get('syncStrategy', 'AUTO')
    if (strategy == 'AUTO' and config.get('direction') != 'bidirectional'
            and not config.get('updateOnly') and method_name(config) == 'size'):
        return 'COMPARE_SIZE'
    return strategy
