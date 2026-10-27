"""TPM（每分钟词元）快照复制：常见数据走快速路径，始终保留独立容器。"""
from __future__ import annotations

import copy


_SCALARS = frozenset((type(None), bool, int, float, str))
_MISSING = object()


def clone_snapshot(value, memo=None):
    """保留 deepcopy（深复制）的别名、循环及自定义复制语义。

    只优化精确的内建字典、列表和 JSON（数据交换格式）原子类型；子类与
    其他对象仍交给标准库，并共享同一个 memo（已复制对象表）。任何新建的
    可变容器都不与输入共享，调用者可以独立修改返回快照。
    """
    return _clone(value, {} if memo is None else memo)


def _clone(value, memo):
    kind = type(value)
    if kind in _SCALARS:
        # A caller/custom copier can explicitly memoize even an immutable value.
        # Honor that mapping without the generic deepcopy dispatch for each atom.
        return memo.get(id(value), value)
    identifier = id(value)
    previous = memo.get(identifier, _MISSING)
    if previous is not _MISSING:
        return previous
    if kind is dict:
        result = {}
        memo[identifier] = result
        for key, item in value.items():
            # Match standard deepcopy's value-before-key evaluation order.
            result[_clone(key, memo)] = _clone(item, memo)
    elif kind is list:
        result = []
        memo[identifier] = result
        for item in value:
            result.append(_clone(item, memo))
    else:
        return copy.deepcopy(value, memo)
    memo[identifier] = result
    # Keep originals alive for the lifetime of memo, as deepcopy does. This also
    # protects against identifier reuse when a custom copier creates temporaries.
    try:
        memo[id(memo)].append(value)
    except KeyError:
        memo[id(memo)] = [value]
    return result
