"""任务状态机：单一状态入口，禁止用多个布尔字段隐式推断任务状态。

为什么需要它：
- 旧实现里 `status`、`phase`、`stopped_at`、`finished_at` 分散在各处直接赋值，
  “部分完成 / 失败 / 已停止 / 结果缺失”之间没有明确边界。
- 任何新状态都只能在 STATES 中声明，并显式写出允许的迁移路径。

状态语义（面向“报告可信度”而非面向“进程”）：
- draft      草稿：已保存配置，尚未提交执行。
- waiting    排队中：已提交，等待前序任务释放执行位。
- running    运行中：正在产生请求与结果。
- stopping   停止中：已发出停止信号，在途请求仍会完成并落盘。
- finished   已完成：全部计划请求完成，且没有失败。
- finished_with_errors 部分完成：跑完计划量，但存在失败或请求错误。
- stopped    已停止：人工中断，结果只覆盖已完成部分。
- error      失败：执行过程异常终止，结果可能不完整。
- expired    已过期：结果文件缺失或校验不一致，不能作为可信证据。
- archived   已归档：保留只读记录，不再出现在默认列表。
"""
import time

STATES = (
    "draft", "waiting", "running", "stopping",
    "finished", "finished_with_errors", "stopped", "error",
    "expired", "archived",
)

TERMINAL_STATES = frozenset({"finished", "finished_with_errors", "stopped", "error", "expired", "archived"})

STATUS_LABELS = {
    "draft": "草稿",
    "waiting": "排队中",
    "running": "运行中",
    "stopping": "停止中",
    "finished": "已完成",
    "finished_with_errors": "部分完成",
    "stopped": "已停止",
    "error": "失败",
    "expired": "结果已过期",
    "archived": "已归档",
}

# 允许的迁移。键为当前状态，值为可迁移到的目标状态集合。
# 说明：
# - 旧数据里存在 "finished" 任务被人工停止的情形，因此保留 finished -> stopped。
# - 任何任务都可以进入 expired / archived，因为这是对“证据可用性”的重新判定，
#   而不是对执行过程的重新判定。
_ALLOWED = {
    "draft": {"waiting", "running", "stopped", "error", "archived"},
    "waiting": {"running", "stopping", "stopped", "error", "expired", "archived"},
    "running": {"stopping", "finished", "finished_with_errors", "stopped", "error", "expired"},
    "stopping": {"finished", "finished_with_errors", "stopped", "error", "expired"},
    "finished": {"stopped", "expired", "archived", "finished_with_errors"},
    "finished_with_errors": {"stopped", "expired", "archived", "finished"},
    "stopped": {"expired", "archived", "finished_with_errors"},
    "error": {"expired", "archived"},
    "expired": {"archived"},
    "archived": {"expired"},
}


def transition_allowed(source, target):
    """判断状态迁移是否合法。未知状态一律拒绝，避免拼写错误静默生效。"""
    if source not in _ALLOWED or target not in _ALLOWED:
        return False
    if source == target:
        return True
    return target in _ALLOWED[source]


class StatusTransitionError(ValueError):
    """非法的状态迁移。调用方应把它当作编程错误，而不是用户输入错误。"""


def apply_transition(holder, target, *, at=None, force=False):
    """把 `holder.status` 迁移到 `target`，并维护 started_at / finished_at / stopped_at。

    `holder` 需要具备 status / started_at / finished_at / stopped_at / created 属性。
    `force=True` 供历史数据恢复使用：旧记录可能跨越了当前定义的迁移路径，
    此时只做归一化，不因为迁移表而拒绝加载。
    返回是否真的发生了状态变化。
    """
    target = str(target or "").strip()
    if target not in STATES:
        raise StatusTransitionError(f"未知任务状态：{target!r}")
    current = str(getattr(holder, "status", "") or "")
    if not force and not transition_allowed(current, target):
        raise StatusTransitionError(f"不允许的状态迁移：{current!r} -> {target!r}")
    moment = time.time() if at is None else float(at)
    changed = current != target
    holder.status = target
    if target == "running" and not getattr(holder, "started_at", None):
        holder.started_at = moment
    if target in TERMINAL_STATES and not getattr(holder, "finished_at", None):
        holder.finished_at = moment
        if target == "stopped" and not getattr(holder, "stopped_at", None):
            holder.stopped_at = moment
    return changed


def normalize_terminal_state(status, *, done=0, total=0, has_errors=False, stopped=False, failed=False):
    """根据执行结果归一化终态，替代散落的嵌套三元表达式。

    优先级：失败 > 停止 > 部分完成 > 已完成。

    关键区分（容易被三元表达式写错的地方）：
    - 计划量没跑完且没出过错：是「已停止」。即使没显式按停止键，
      结果覆盖不到计划量同样是“中断”，不能用「完成但有错误」冒充已跑完。
    - 跑完但确有非成功请求：才是「部分完成（完成但有错误）」。
    - 计划总量未知（total=0）时不判定完成度，只按有无错误定级。
    """
    if failed:
        return "error"
    if stopped:
        return "stopped"
    if has_errors:
        return "finished_with_errors"
    if total and done < total:
        return "stopped"
    return "finished"


def status_label(status):
    return STATUS_LABELS.get(status, status or "未知")
