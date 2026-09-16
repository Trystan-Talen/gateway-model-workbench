"""压测方案模板：把「怎么压」沉淀成可复用、可评审的对象。

为什么需要它：
- 现在每次发起压测都要手填并发、调度速率、时长、超时。填错一个数字，
  结论就从「服务扛不住」变成「我配错了」，而报告本身看不出这个区别。
- 复测时应该跑同一个方案，而不是凭记忆重新填一遍。

模板只描述「怎么压」，不保存密钥：任何写入口都会剥掉 api_key 及同类字段（见 `sanitize`）。
"""
from __future__ import annotations

import json
import os
import threading
import time

TEMPLATES_FILE = "test_templates.json"

# 内置模板是「起步方案」，不是「推荐容量」：它们定义的是形状（并发/速率/时长），
# 不是结论。每个模板都写明适用场景和不该拿它下什么结论。
BUILTIN = (
    {
        "id": "smoke-baseline",
        "name": "冒烟基线",
        "category": "smoke",
        "summary": "小并发、固定条数，确认链路、鉴权和响应格式正常。",
        "when": "换网关、换模型、改配置之后的第一步。",
        "not_for": "不能用来判断容量：条数太少，且没有压力档位。",
        "config": {"mode": "smoke", "concurrency": 2, "rpm": 120, "iterations": 30,
                   "connect_timeout": 10, "read_timeout": 60, "profile": "smoke"},
    },
    {
        "id": "smoke-verify-10x",
        "name": "冒烟+10 倍复测",
        "category": "smoke",
        "summary": "在冒烟的基础上把条数放大到 300，用于暴露偶发失败。",
        "when": "冒烟通过后、正式压测之前，确认不是运气好。",
        "not_for": "并发没变，仍然不能反映并发下的表现。",
        "config": {"mode": "smoke", "concurrency": 4, "rpm": 300, "iterations": 300,
                   "connect_timeout": 10, "read_timeout": 60, "profile": "smoke"},
    },
    {
        "id": "ramp-find-knee",
        "name": "阶梯找拐点",
        "category": "ramp",
        "summary": "并发按阶梯抬升，观察成功率、实际完成吞吐和 p95 在哪一档开始恶化。",
        "when": "需要给容量一个量级判断，并指出拐点大致落在哪一档。",
        "not_for": "台阶之间不是稳态，拐点位置是区间而不是精确值。",
        "config": {"mode": "ramp", "concurrency": 4, "ramps": "120,240,360,480,600", "duration": 180,
                   "connect_timeout": 10, "read_timeout": 90, "profile": "smoke"},
    },
    {
        "id": "ramp-conservative",
        "name": "阶梯找拐点（保守）",
        "category": "ramp",
        "summary": "更平的阶梯、更长的每档时长，适合还没摸过底的线上网关。",
        "when": "第一次对某个环境做压测，且不确定它能不能扛。",
        "not_for": "耗时更长；如果只是回归验证，用它属于浪费。",
        "config": {"mode": "ramp", "concurrency": 2, "ramps": "60,120,240,360", "duration": 300,
                   "connect_timeout": 10, "read_timeout": 120, "profile": "smoke"},
    },
    {
        "id": "soak-30m",
        "name": "稳态泡测 30 分钟",
        "category": "soak",
        "summary": "在固定并发和速率下持续跑，观察长尾延迟、失败率与内存类问题是否随时间漂移。",
        "when": "怀疑泄漏、连接池耗尽、长时间运行才出现的退化。",
        "not_for": "单次泡测不能替代多轮重复；漂移需要重复运行才能确认。",
        "config": {"mode": "soak", "concurrency": 8, "rpm": 240, "duration": 1800,
                   "connect_timeout": 10, "read_timeout": 120, "profile": "smoke"},
    },
    {
        "id": "soak-short-10m",
        "name": "稳态泡测 10 分钟",
        "category": "soak",
        "summary": "泡测的精简版，用于在 CI 或本地做一次趋势检查。",
        "when": "只想看有没有明显漂移，不想等半小时。",
        "not_for": "10 分钟不足以暴露慢速泄漏。",
        "config": {"mode": "soak", "concurrency": 6, "rpm": 180, "duration": 600,
                   "connect_timeout": 10, "read_timeout": 120, "profile": "smoke"},
    },
)

# 与结果/密钥相关的字段一律不进入模板文件。
_SECRET_FIELDS = {"api_key", "apikey", "authorization", "token", "password", "secret"}
# 运行期字段不属于方案：它们由服务端在创建任务时决定。
_VOLATILE_FIELDS = {"risk_acknowledged", "_suite_cases", "suite_manifest"}


def sanitize(cfg):
    """剥掉密钥与运行期字段，只保留可复用的方案字段。"""
    out = {}
    for key, value in (cfg or {}).items():
        normalized = str(key).strip().lower().replace("-", "_")
        if normalized in _SECRET_FIELDS or normalized in _VOLATILE_FIELDS:
            continue
        if value is None:
            continue
        out[key] = value
    return out


class TemplateStore:
    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATES_FILE)
        self.lock = threading.RLock()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        items = data.get("templates") if isinstance(data, dict) else data
        return [item for item in (items or []) if isinstance(item, dict)]

    def _write(self, items):
        temp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with self.lock:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"templates": items}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list(self):
        """内置模板 + 用户保存的模板。用户模板只能覆盖同 id 的内置模板。"""
        builtin_ids = {item["id"] for item in BUILTIN}
        merged = []
        for item in BUILTIN:
            merged.append({**item, "builtin": True})
        for item in self._read():
            entry = dict(item)
            entry["builtin"] = entry.get("id") in builtin_ids
            merged.append(entry)
        return merged

    def get(self, template_id):
        return next((item for item in self.list() if item.get("id") == template_id), None)

    def save(self, payload):
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            raise ValueError("模板需要名称")
        config = sanitize((payload or {}).get("config"))
        if not config:
            raise ValueError("模板需要 config")
        template_id = str((payload or {}).get("id") or "").strip()
        if not template_id:
            template_id = f"custom-{int(time.time())}-{len(self._read())}"
        entry = {
            "id": template_id,
            "name": name,
            "category": str((payload or {}).get("category") or "custom"),
            "summary": str((payload or {}).get("summary") or ""),
            "when": str((payload or {}).get("when") or ""),
            "not_for": str((payload or {}).get("not_for") or ""),
            # 保存时间用于判断模板是否已经跟不上当前配置字段。
            "saved_at": time.time(),
            "config": config,
        }
        with self.lock:
            items = [item for item in self._read() if item.get("id") != template_id]
            items.append(entry)
            self._write(items)
        return entry

    def delete(self, template_id):
        builtin_ids = {item["id"] for item in BUILTIN}
        if template_id in builtin_ids:
            raise ValueError("内置模板不可删除")
        with self.lock:
            items = self._read()
            remaining = [item for item in items if item.get("id") != template_id]
            if len(remaining) == len(items):
                return False
            self._write(remaining)
        return True
