#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能路由 vs 固定路由对照测试的**专用测试集**（不是质量评测题集）。

为什么不能用质量题集
--------------------
质量评测的 120 道题是给人看的诊断题：题面各异、彼此独立、每题只跑一次。
把它搬来做路由对照，会同时踩三个坑：

1. **缓存纹理会归零**。逐题内容互不相同，前缀不复用，两条路由都拿不到缓存命中，
   于是「缓存命中率差多少」这个本次最关心的指标，在数据里恒等于 0 —— 测了个寂寞。
2. **样本量不够**。120 条摊到「1000/3000/5000」三档里根本不够分，分档也就失去意义。
3. **题目难度差异会污染结论**。两侧跑的不是同一批等价请求，差异说不清是路由造成的
   还是题目造成的。

本测试集的设计目标
------------------
- **确定性可复现**：固定随机种子 + 固定语料版本，任何人任何时间生成，逐字节一致；
  报告里因此可以写明「测试集版本」，让两次实验真的可比。
- **让缓存真的有戏**：请求按「段（burst）」组织，同一段内共享一段很长的固定前缀
  （模拟真实业务里反复携带的同一份系统提示词 / 同一份文档），只有结尾一小段不同。
  同一个后端（channel）连续处理同一段请求时，第二次起就能命中前缀缓存。于是
  「固定路由 = 一个后端连续吃完整段 → 命中率高」与「智能路由 = 按稳定性择优切换，
  同一段被摊到多个后端 → 各自缓存各自冷启动 → 命中率下降」这个差异，才会**真的出现
  在数据里**，而不是消失在噪声里。
- **分档可选且严格可比**：1000 / 3000 / 5000 三档不是三份随机题，而是同一份主清单的
  前 N 条。高档位的请求集**包含**低档位的请求集，且同一条请求的 ``item_id`` 与内容
  完全一致 —— 这样跨档位对比的是「同一批请求跑得更多」而不是「换了一批题」。
- **A/B 可配对**：每条请求有稳定的 ``item_id``。两侧跑完后按 ``item_id`` 做配对比较
  （同一条请求在两条路由上的结果直接相减），而不是只比两个总量平均数 —— 后者会把
  请求之间的固有差异算进路由差异里。

诚实的边界（必须写进报告，不能假装没有）
--------------------------------------
- 本测试集能测出的是**「在共享前缀的场景下，缓存命中率差多少」**，不是「路由算法好不好」。
  如果智能路由把同一段摊到 10 个后端，命中率就该按 1/10 量级下降 —— 这是**设计使然、
  不是缺陷**。报告的价值在于把这个代价量化出来，交给决策者去权衡「稳定性收益 vs 缓存成本」。
- 缓存是否命中，只能从上游返回的 ``usage.cached_tokens`` 读出来（平台自身也是这个口径）。
  上游不回 usage 时，命中率**不可得**，必须如实标注为「不可得」而不是 0 —— 详见
  ``smart_route_load.py`` 与 ``analyze.py`` 里的同一条注释。
- 本测试集不构造任何真实业务数据，也不含密钥；它只是一批可复现的合成请求。

数据结构
--------
本模块只生成**请求规格**（prompt 文本 + 元数据），不发网络请求、不碰密钥。
实际执行由 ``smart_route_load.py`` 完成。
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Dict, Iterator, List, Sequence

# 语料版本：任何会改变生成结果的改动都必须同步递增此版本号，并在报告里写明。
# 版本号不同 = 两份测试集不可比，这不是提醒，是硬约束。
CORPUS_VERSION = "routing-ab-v1"

# 固定随机种子：保证「同一版本、同一档位」在任何机器上生成完全相同的请求。
CORPUS_SEED = 20240914

# 可选档位。每档都是主清单的前 N 条，因此高档位严格包含低档位。
TIERS: Sequence[int] = (1000, 3000, 5000)

# 段大小：同一段内的请求共享同一段长前缀。20 条一换，是为了让「后端连续处理同一段」
# 这个前提在中等并发下也能成立；段太大则切换路由带来的缓存损失会被稀释得看不清。
BURST_SIZE = 20

# 前缀规模档位（字符数近似值）。模拟真实业务里不同长度的系统提示词 / 文档上下文。
# 短前缀缓存容易整段命中，长前缀更容易触发分块缓存与淘汰 —— 两种都要覆盖。
PREFIX_LENGTHS = (600, 1600, 3600)

# 结尾差异化的问题模板。每条请求的结尾都不同，确保「不是完全相同的请求」，
# 避免被当成幂等重放而被上游或网关去重。
TAIL_TEMPLATES = (
    "请用不超过三句话回答：第 {n} 号巡检项当前是否达标？只给出结论与一句依据。",
    "把下面这条状态改写成一条可执行工单：编号 {n} 的服务今日错误率上升 0.3%。",
    "针对编号 {n} 的接口，列出两条最值得优先看的监控指标，每行一条。",
    "编号 {n} 的请求出现偶发超时，请给出一个不依赖重试的排查方向。",
    "用一句话总结编号 {n} 的这批数据最反常的地方。",
    "编号 {n}：把‘偶尔有点慢’改写成可度量的故障描述，不超过四十字。",
    "编号 {n} 的接口返回 200 但正文被截断，应计为成功吗？一句话说明。",
    "编号 {n}：请给出一条最简单的输入校验规则，不超过三十字。",
    "编号 {n} 的服务连续两分钟错误率上升，先查哪个方向？一句话。",
    "编号 {n}：请只输出一个数字，表示本次是否达标（达标填 1，否则填 0）。",
)

# 前缀语料的素材池。这些段落会被拼成「同一段内完全相同的长前缀」。
# 内容本身是通用工程语料，不含任何真实业务数据，也不含凭据。
_PREFIX_MATERIAL = (
    "服务基线：该接口在稳定负载下总耗时 p95 应低于三百毫秒，任何一分钟窗口内完整成功响应比例不得低于百分之九十九点九。",
    "观测口径：限流、超时、网络失败与正文不完整必须分开统计，不得合并成一个笼统的失败率。",
    "容量约束：应用连接数不随请求并发线性增长，缓存失效时也不允许所有请求同时回源。",
    "一致性要求：普通读取允许五秒以内的最终一致，提交前的确认必须读取权威数据。",
    "重试纪律：重试必须限制次数并加入抖动，超出预算后返回可识别的错误，不允许无限重试掩盖失败。",
    "发布策略：迁移按百分之一、百分之十、百分之五十与全量四阶段推进，每阶段至少观察一个完整高峰。",
    "日志要求：每个请求都带全链路标识，服务端记录接口名、结果类别与耗时分段，但不写入访问密钥。",
    "降级原则：客户端、应用层与存储层都要有明确的背压与降级策略，不能把压力全留给最下游。",
    "验收口径：报告同时展示计划调度速率、实际完成吞吐、成功率、首字耗时与完整响应耗时。",
    "异常场景：需覆盖缓存不可用、单区域网络抖动、下游变慢、消息重复投递与时钟偏差。",
    "回滚原则：回滚只切换读取入口，已经创建的资源占用仍由新版本处理完毕，避免中途状态丢失。",
    "可观测性：任何结论都必须来自客户端实际观测到的状态，不得根据响应文本猜测内部选路。",
)


_PREFIX_CACHE: Dict[tuple, str] = {}


def _prefix_material_for(burst_index: int, total_chars: int) -> str:
    """为某个段生成一段**确定性**的长前缀，长度约等于 total_chars。

    同一段内所有请求拿到完全相同的前缀 —— 这正是缓存能命中的前提。
    不同段之间前缀不同 —— 避免整轮请求共用一个前缀，那样两条路由都会接近满命中，
    差异同样会被压平。

    结果按段缓存：同段内 20 条请求拼的是同一段字符串，没必要重复生成。
    """
    key = (burst_index, total_chars)
    cached = _PREFIX_CACHE.get(key)
    if cached is not None:
        return cached
    seeded = random.Random(f"{CORPUS_SEED}:{burst_index}")
    pieces: List[str] = []
    size = 0
    counter = 0
    while size < total_chars:
        paragraph = _PREFIX_MATERIAL[(burst_index + counter) % len(_PREFIX_MATERIAL)]
        # 段编号写进正文，确保不同段的前缀字节不同（否则内容相同的段落会互相命中）。
        line = f"（第 {burst_index} 段 · 第 {counter + 1} 条基线资料）{paragraph}"
        pieces.append(line)
        size += len(line)
        counter += 1
        if counter % 6 == 0:
            # 每隔几段插入一个该段专属的噪声词，进一步区分不同段的前缀。
            token = "".join(seeded.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(8))
            pieces.append(f"段内校验码：{token}")
    result = "\n".join(pieces)
    # 上限保护：语料本身有限，档位再多也不会无限增长（每档最多几百段）。
    if len(_PREFIX_CACHE) < 4096:
        _PREFIX_CACHE[key] = result
    return result


def _item_id(index: int) -> str:
    """稳定 item_id：只由全局序号决定，与档位无关，供 A/B 配对使用。

    稳定是硬需求 —— ``item_id`` 一变，两侧就配不上对，只能退化成比平均数的粗略比较。
    """
    return f"{CORPUS_VERSION}-{index:06d}"


def build_item(index: int) -> Dict[str, Any]:
    """按全局序号生成一条请求规格。同一 index 永远得到同一条请求。"""
    burst_index = index // BURST_SIZE
    prefix_chars = PREFIX_LENGTHS[burst_index % len(PREFIX_LENGTHS)]
    prefix = _prefix_material_for(burst_index, prefix_chars)
    tail = TAIL_TEMPLATES[index % len(TAIL_TEMPLATES)].format(n=index)
    content = f"{prefix}\n\n{tail}"
    return {
        "item_id": _item_id(index),
        "index": index,
        "burst_index": burst_index,
        "burst_size": BURST_SIZE,
        "shared_prefix_chars": len(prefix),
        "prompt": content,
        # 前缀指纹：用于离线核对「同一段内前缀确实相同」，也是报告里可自查的证据。
        "prefix_sha256": hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:16],
    }


def build_corpus(tier: int) -> List[Dict[str, Any]]:
    """生成指定档位的请求清单。

    ``tier`` 必须是 ``TIERS`` 之一。返回的是主清单的前 ``tier`` 条，因此 1000 档的
    请求集是 3000 档的前缀子集，跨档位比较的始终是同一批请求。
    """
    tier = int(tier)
    if tier not in TIERS:
        raise ValueError(f"档位必须是 {list(TIERS)} 之一，收到 {tier}")
    return [build_item(index) for index in range(tier)]


def iter_corpus(tier: int) -> Iterator[Dict[str, Any]]:
    """流式生成，避免一次性占用内存（5000 条长前缀约为若干 MB 量级）。"""
    tier = int(tier)
    if tier not in TIERS:
        raise ValueError(f"档位必须是 {list(TIERS)} 之一，收到 {tier}")
    for index in range(tier):
        yield build_item(index)


def corpus_manifest(tier: int) -> Dict[str, Any]:
    """测试集身份摘要：报告里必须带上它，否则两次实验不可比。

    只包含可公开的元数据 —— 不包含 prompt 正文，避免把大段文本塞进历史索引。
    """
    tier = int(tier)
    if tier not in TIERS:
        raise ValueError(f"档位必须是 {list(TIERS)} 之一，收到 {tier}")
    digest = hashlib.sha256()
    for index in range(tier):
        digest.update(_item_id(index).encode("utf-8"))
    return {
        "id": CORPUS_VERSION,
        "version": 1,
        "tier": tier,
        "burst_size": BURST_SIZE,
        "burst_count": (tier + BURST_SIZE - 1) // BURST_SIZE,
        "prefix_length_cycle": list(PREFIX_LENGTHS),
        "item_id_digest": digest.hexdigest()[:16],
        "method": "同一档位为主清单前缀；同段共享长前缀以激活前缀缓存",
    }


def tier_summary(tier: int) -> Dict[str, Any]:
    """给界面用的档位说明：条数、段数、预计前缀复用次数。"""
    tier = int(tier)
    if tier not in TIERS:
        raise ValueError(f"档位必须是 {list(TIERS)} 之一，收到 {tier}")
    bursts = (tier + BURST_SIZE - 1) // BURST_SIZE
    return {
        "tier": tier,
        "bursts": bursts,
        "burst_size": BURST_SIZE,
        "note": f"{tier} 条请求，按每 {BURST_SIZE} 条一段共享长前缀；同段内第 2 条起才可能命中缓存。",
    }


def all_tier_summaries() -> List[Dict[str, Any]]:
    return [tier_summary(tier) for tier in TIERS]


def main() -> None:
    """命令行入口：生成测试集供人工核对（默认只打印摘要，不打印正文）。"""
    import argparse

    parser = argparse.ArgumentParser(description="生成智能路由 vs 固定路由对照测试集")
    parser.add_argument("--tier", type=int, default=1000, choices=list(TIERS))
    parser.add_argument("--out", help="可选：把完整清单写成 JSONL（逐行 JSON）")
    parser.add_argument("--show", type=int, default=0, help="打印前 N 条请求前缀预览")
    args = parser.parse_args()

    manifest = corpus_manifest(args.tier)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if args.out:
        count = 0
        with open(args.out, "w", encoding="utf-8") as handle:
            for item in iter_corpus(args.tier):
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                count += 1
        print(f"已写入 {count} 条到 {args.out}")
    for index in range(min(args.show, args.tier)):
        item = build_item(index)
        preview = item["prompt"][:60].replace("\n", " ")
        print(f"{item['item_id']}  段={item['burst_index']:<4} 前缀={item['shared_prefix_chars']:<5} {preview}…")


if __name__ == "__main__":
    main()
