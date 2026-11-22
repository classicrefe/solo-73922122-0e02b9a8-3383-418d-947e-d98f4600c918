"""风电限发损失归因入仓。"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """惰性导入，避免纯算法测试时加载 Flask。"""
    from .app import create_app as factory

    return factory(*args, **kwargs)
