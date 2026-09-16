"""区分度与敏感性分析：这份卷子能不能把两个资源分开。

起因：glm-5.3 与 deepseek-flash 在 120 题上同为 117/120，报告只说「分数接近」。
但 114 题同分、两侧各自的失败题完全不重叠 —— 分数相同并不等于表现相同。
本模块只做一件事：把「同分」拆成可核对的三类证据，并给出这份卷子
在统计上最小能分辨多大的分差。它不改变主分、不做准入判断。

所有计算只用已保存的逐题证据，不猜分、不补题：

- 配对（McNemar 口径）：只看两侧结果不一致的题。一致的题对「谁更好」没有信息，
  但能说明卷子在哪些维度已经到顶。
- 最小可辨差：在不一致题数固定的前提下，用正态近似给出 80% 功效下
  能检出的分差下限，以及 95% 置信区间。它是一个「本轮数据不足以判断」的量，
  不是把不显著粉饰成等效。
- 饱和度：单维度内所有题都要通过，才谈得上比较；10 题维度 100% 对 90%
  在 n=10 时也到不了显著，因此维度分天然只有 10 分一档。
"""

from __future__ import annotations

from collections import defaultdict
import math

# 配对比较的显著性水平与功效；都用标准值，不引入可调旋钮。
_ALPHA = 0.05
_POWER = 0.80
_Z_ALPHA = 1.959963984540054  # 双侧 95%
_Z_POWER = 0.8416212335729143  # 80% 功效

BAND_ORDER = ("结果完全一致", "只在少数题上不一致", "有区分", "区分度良好")


def _paired(counts):
    """配对计数：a_wins 只 A 过、b_wins 只 B 过、ties 同结果、both_failed 都不过。"""
    d = dict(counts or {})
    return (
        int(d.get("a_wins") or 0),
        int(d.get("b_wins") or 0),
        int(d.get("ties") or 0),
        int(d.get("both_failed") or 0),
    )


def mcnemar_two_sided_p(a_wins, b_wins):
    """McNemar 精确检验的双侧 p 值。

    只看不一致题（a_wins 与 b_wins）。一致题再多也不提供优劣信息，因此样本量是
    不一致题数而不是总题数。n=0 时返回 1.0（完全没有信息，不能拒绝任何假设）。
    """
    n = int(a_wins) + int(b_wins)
    if n <= 0:
        return 1.0
    k = min(int(a_wins), int(b_wins))
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def paired_binomial_summary(a_wins, b_wins, pairs):
    """不一致题的胜率差与 95% 置信区间；pairs 为参与配对的总题数。

    McNemar 只看不一致题，因此有效样本是 n = a_wins + b_wins，
    而不是全部 pairs。差值口径与报告一致：百分比，b_wins - a_wins（乙减甲）。
    """
    n = int(a_wins) + int(b_wins)
    total = int(pairs) or n
    if n <= 0 or total <= 0:
        return {
            "discordant": 0,
            "diff_pct": 0.0,
            "ci95_low_pct": None,
            "ci95_high_pct": None,
            "mcnemar_p": 1.0,
            "detectable": False,
        }
    diff = (int(b_wins) - int(a_wins)) / total * 100.0
    se = math.sqrt(n) / total * 100.0
    return {
        "discordant": n,
        "diff_pct": round(diff, 2),
        "ci95_low_pct": round(diff - _Z_ALPHA * se, 2),
        "ci95_high_pct": round(diff + _Z_ALPHA * se, 2),
        "mcnemar_p": round(mcnemar_two_sided_p(a_wins, b_wins), 4),
        "detectable": abs(diff) >= _Z_ALPHA * se,
    }


def minimum_detectable_difference(pairs, p_discordant=0.5):
    """在 pairs 对配对题上，80% 功效能检出的最小分差（百分点）。

    这是设计层面的量，只取决于总配对题数（和不一致题的占比假设），
    与本次实际出现多少不一致题无关 —— 否则会得出「不一致题越少越灵敏」的
    反直觉结论。参考配对比例检验的样本量公式：
    n = (z_(1-α/2)+z_(1-β))^2 · p_disc · (1-p_disc) / δ^2，
    在总题数固定时反解 δ，并在 p_disc 上取 0.5 这一最保守的极大点
    （也就是「最多能分辨多大」的下限），得 δ = 0.5·(z_a+z_b)/sqrt(n)。
    """
    total = int(pairs) or 0
    if total <= 0:
        return None
    p = min(max(float(p_discordant), 0.0), 1.0)
    return round((_Z_ALPHA + _Z_POWER) * math.sqrt(p * (1.0 - p)) / math.sqrt(total) * 100.0, 2)


def _discrimination_band(mcnemar_p, discordant_ratio, detectable):
    if discordant_ratio <= 0:
        return "结果完全一致"
    if mcnemar_p < 0.05 and detectable:
        return "区分度良好" if discordant_ratio >= 0.05 else "有区分"
    if detectable:
        return "有区分"
    return "只在少数题上不一致"


def build_discrimination(pairwise, resources):
    """从配对结果生成区分度证据：同分构成、饱和维度、可辨差下限。"""
    if not isinstance(pairwise, dict):
        return None
    a_wins, b_wins, ties, both_failed = _paired(pairwise)
    pairs = int(pairwise.get("paired_cases") or 0) or (a_wins + b_wins + ties)
    if pairs <= 0:
        return None

    left = resources[0] if len(resources) > 0 else {}
    right = resources[1] if len(resources) > 1 else {}
    binom = paired_binomial_summary(a_wins, b_wins, pairs)
    mdd = minimum_detectable_difference(pairs)
    discordant_ratio = binom["discordant"] / pairs if pairs else 0.0

    # 失败集不重叠：两侧都各自失败、且不是同一批题。失败原因相反时，
    # 总分数相同反而掩盖了「换一个资源会换一批题挂掉」这一事实。
    a_failed = {x.get("case_id") for x in (left.get("failure_evidence") or [])}
    b_failed = {x.get("case_id") for x in (right.get("failure_evidence") or [])}
    disjoint = len(a_failed ^ b_failed) if a_failed and b_failed and not (a_failed & b_failed) else 0

    # 维度饱和：本轮分数低到不可能靠单题波动解释的维度，对这对资源没有信息量。
    saturated, separating = [], []
    for row in pairwise.get("categories") or []:
        paired = int(row.get("paired") or 0)
        if not paired:
            continue
        high = max(row.get("a_score") or 0.0, row.get("b_score") or 0.0)
        low = min(row.get("a_score") or 0.0, row.get("b_score") or 0.0)
        if high >= 99.5 and low >= 99.5:
            saturated.append({"category": row.get("category"), "label": row.get("label"), "a_score": row.get("a_score"), "b_score": row.get("b_score")})
        elif row.get("delta"):
            separating.append({"category": row.get("category"), "label": row.get("label"), "delta": row.get("delta")})

    # 维度只有 10 题，单题错就是 10 分一档；能分辨 10 分差需要的最少题数。
    per_dimension = max((int(r.get("planned") or 0) for r in (left.get("categories") or [])), default=0)
    resolution_floor = math.ceil((2.0 * _Z_ALPHA / 0.10) ** 2 / 4.0) if per_dimension else None

    notes = []
    notes.append(f"{pairs} 对题里 {ties} 对同分，不一致题只有 {binom['discordant']} 对（A 独胜 {a_wins}、B 独胜 {b_wins}）。")
    if both_failed:
        notes.append(f"两侧共同失败 {both_failed} 对；共同失败不能用来比较优劣。")
    if disjoint:
        notes.append(f"两侧失败题互不重叠：{disjoint} 道题分别只在一侧失败，总分相同但失效点不同。")
    if saturated:
        notes.append("以下维度两侧都接近满分，对本对资源已无区分力：" + "、".join(x["label"] for x in saturated[:6]))
    if mdd is not None:
        notes.append(f"{pairs} 对配对题决定了这份卷子的分辨上限：即使不一致题全部偏向一侧，80% 功效下也只能分辨约 {mdd} 个百分点的分差。")
    if binom["discordant"] and binom["discordant"] <= 0.05 * pairs:
        notes.append(f"本轮不一致题只占 {round(discordant_ratio * 100, 2)}%，可用的配对信号很稀疏；这类「分数接近」是卷子到顶，而不是两个资源等价。")
    if resolution_floor is not None:
        notes.append(f"每维度 {per_dimension} 题、逐题 0/1 计分，维度分天然只有约 {round(100.0 / per_dimension, 1)} 分一档；要稳定分辨 10 分档每维度至少约 {resolution_floor} 题。")

    if ties == pairs:
        verdict = "完全一致，无法比较"
        basis = "全部配对题结果相同；这份卷子在这两个资源之间没有产生任何不一致证据。"
    elif binom["detectable"] and binom["mcnemar_p"] < 0.05:
        verdict = "不一致题足以指向一侧"
        basis = f"不一致题 {binom['discordant']} 对，McNemar 精确检验 p={binom['mcnemar_p']}。"
    else:
        verdict = "分数接近，且卷子无法分辨"
        basis = (f"不一致题 {binom['discordant']} 对，McNemar 精确检验 p={binom['mcnemar_p']}，"
                 f"未达到可判定水平：这份卷子无法分辨两者。")

    return {
        "title": "区分度与敏感性",
        "pairs": pairs,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "ties": ties,
        "both_failed": both_failed,
        "discordant": binom["discordant"],
        "discordant_ratio_pct": round(discordant_ratio * 100, 2),
        "diff_pct": binom["diff_pct"],
        "ci95_pct": [binom["ci95_low_pct"], binom["ci95_high_pct"]],
        "mcnemar_p": binom["mcnemar_p"],
        "detectable": bool(binom["detectable"]),
        "min_detectable_delta_pct": mdd,
        "score_resolution_pct": round(100.0 / per_dimension, 2) if per_dimension else None,
        "per_dimension_cases": per_dimension,
        "cases_needed_for_10pct_dimension": resolution_floor,
        "band": _discrimination_band(binom["mcnemar_p"], discordant_ratio, binom["detectable"]),
        "disjoint_failure_cases": disjoint,
        "saturated_categories": saturated,
        "separating_categories": separating,
        "verdict": verdict,
        "basis": basis,
        "notes": notes,
        "scope": "只描述本轮题集对这两个资源的分辨能力，不改变主分，也不构成等效或优劣结论。",
    }


def overlay(snapshot):
    """把区分度证据写进报告；无配对信息时不做任何改动。"""
    if not isinstance(snapshot, dict):
        return snapshot
    discrimination = build_discrimination(snapshot.get("pairwise"), snapshot.get("resources") or [])
    if discrimination:
        snapshot["discrimination"] = discrimination
        layers = snapshot.get("layers")
        if isinstance(layers, dict) and isinstance(layers.get("statistics"), dict):
            layers["statistics"].setdefault("notes", []).append(
                "区分度：" + discrimination["basis"] + (
                    f" 可辨差下限约 {discrimination['min_detectable_delta_pct']} 个百分点。"
                    if discrimination.get("min_detectable_delta_pct") is not None else ""
                )
            )
    return snapshot


__all__ = [
    "BAND_ORDER",
    "build_discrimination",
    "mcnemar_two_sided_p",
    "minimum_detectable_difference",
    "overlay",
    "paired_binomial_summary",
]
