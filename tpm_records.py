"""完成请求的紧凑内存存档；完整证据只编码一次，热缓存有明确上限。"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, Sequence
import json
import operator
import threading


_DISPLAY_FIELDS = ('id', 'phase', 'stage_index', 'success', 'error', 'error_detail',
                   'output_limited', 'timing')
_DISPLAY_USAGE_FIELDS = ('full_input_tokens', 'output_tokens', 'complete', 'completeness')


def project_request_summary(record):
    """选择当前请求表展示字段；最终快照边界仍须独立复制所有可变容器。

    这是浅层投影，不改变完成记录存档或计量依据。字段按存在性保留，不能
    将缺失补成零或空值。旧记录没有标准时间/用量，或含逐次尝试等扩展时，
    保守保留完整内容，避免损失旧界面能够显示的诊断证据。
    """
    if (type(record) is not dict or not isinstance(record.get('timing'), dict)
            or record['timing'].get('source') == 'legacy'
            or not isinstance(record.get('usage'), dict)
            or any(key in record for key in ('attempts', 'truncated', 'request_id',
                                             'full_input_tokens', 'output_tokens'))):
        return record
    result = {key: record[key] for key in _DISPLAY_FIELDS if key in record}
    result['usage'] = {key: record['usage'][key] for key in _DISPLAY_USAGE_FIELDS if key in record['usage']}
    return result


class CompletedRecords(Sequence):
    """只追加的完整请求记录，冷热访问都返回原生 JSON（数据格式）对象。

    append（追加）转移已完成字典的所有权：调用方与所有读取方此后不得修改
    该字典及其子容器。最近 hot_limit（热缓存上限）条保留原字典，旧条目按需
    从无损 JSON 字符串恢复；返回给界面等可修改的快照仍须独立复制。

    输入契约是引擎产生的原生字典、字符串键、列表与标量，不在此改变原始
    用量的校验政策。编码选项与既有写入器相同，包括非有限数值诊断证据。
    不依赖磁盘，不删除证据，不压缩数值、不修改用量口径。append 返回不含
    换行的编码正文，可直接复用于已有逐行写入器。编码及校验失败时不改变
    容器。切片与迭代捕获调用时已有的范围，后续追加不会延长本次遍历。
    """

    def __init__(self, *, hot_limit=100):
        if type(hot_limit) is not int or hot_limit < 0:
            raise ValueError("热缓存上限必须是非负整数")
        self._hot_limit = hot_limit
        self._encoded = []
        self._hot = OrderedDict()
        self._lock = threading.RLock()

    @property
    def hot_count(self):
        with self._lock:
            return len(self._hot)

    @property
    def hot_limit(self):
        return self._hot_limit

    def append(self, record):
        if type(record) is not dict:
            raise TypeError("完成请求记录必须是字典")
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            index = len(self._encoded)
            self._encoded.append(encoded)
            if self._hot_limit:
                self._hot[index] = record
                if len(self._hot) > self._hot_limit:
                    self._hot.popitem(last=False)
        return encoded

    def retain_unencoded(self, record):
        """编码失败停发路径专用：保留已完成证据，不声称该条已编码或落盘。

        该罕见路径允许完整字典长期驻留，不能丢弃已经结算的请求。调用方仍
        须触发持久化错误并停止新请求；本方法不会把异常证据送给写入器。
        """
        if type(record) is not dict:
            raise TypeError("完成请求记录必须是字典")
        with self._lock:
            index = len(self._encoded)
            self._encoded.append(record)
            if self._hot_limit:
                self._hot[index] = record
                if len(self._hot) > self._hot_limit:
                    self._hot.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._encoded)

    @staticmethod
    def _decode(value):
        return json.loads(value) if type(value) is str else value

    def __getitem__(self, index):
        with self._lock:
            size = len(self._encoded)
            if isinstance(index, slice):
                selected = [self._hot.get(position, self._encoded[position])
                            for position in range(*index.indices(size))]
            else:
                position = operator.index(index)
                if position < 0:
                    position += size
                if not 0 <= position < size:
                    raise IndexError("完成请求记录索引越界")
                selected = self._hot.get(position, self._encoded[position])
        if isinstance(index, slice):
            return [self._decode(value) for value in selected]
        return self._decode(selected)

    def __iter__(self) -> Iterator[dict]:
        with self._lock:
            selected = [self._hot.get(index, value) for index, value in enumerate(self._encoded)]
        # Decode lazily, while the immutable encoded strings and hot ownership
        # references keep this exact iteration independent of later appends.
        return (self._decode(value) for value in selected)
