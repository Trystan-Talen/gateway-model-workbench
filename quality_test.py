"""本地、确定性的资源实例质量测试评分器。

本模块只负责加载题集和评估已经拿到的响应，不创建 HTTP（超文本传输协议）
客户端、不读取 API Key（应用程序编程接口密钥），也不发起网络请求。
"""

from __future__ import annotations

import json
import hashlib
import unicodedata
import math
import re
import uuid
from quality_methodology import methodology_snapshot
from dataclasses import dataclass, field
from datetime import datetime
from time_record import SINGAPORE_TIMEZONE, singapore_timestamp
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_MODULE_DIR = Path(__file__).resolve().parent
_DEFAULT_SUITE_PATH = _MODULE_DIR / "quality_suite.json"


class SuiteFormatError(ValueError):
    """题集文件结构不符合约定。"""


@dataclass(frozen=True)
class QualityCase:
    """一条质量题目及其确定性评分规则。"""

    id: str
    category: str
    prompt: str
    evaluator: Mapping[str, Any]
    request: Mapping[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    title: str = ""
    difficulty: str = "未标注"
    reference_answer: Any = None
    rationale: str = ""
    critical: bool = False
    negative_examples: tuple = ()
    benchmark_refs: tuple = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int = 0) -> "QualityCase":
        if not isinstance(raw, Mapping):
            raise SuiteFormatError(f"cases[{index}] 必须是对象")

        def required_string(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise SuiteFormatError(f"cases[{index}].{name} 必须是非空字符串")
            return value

        case_id = required_string("id")
        category = required_string("category")
        prompt = required_string("prompt")
        evaluator = raw.get("evaluator")
        if not isinstance(evaluator, Mapping):
            raise SuiteFormatError(f"cases[{index}].evaluator 必须是对象")
        evaluator_type = evaluator.get("type")
        if not isinstance(evaluator_type, str) or not evaluator_type.strip():
            raise SuiteFormatError(f"cases[{index}].evaluator.type 必须是非空字符串")

        request = raw.get("request", {})
        if not isinstance(request, Mapping):
            raise SuiteFormatError(f"cases[{index}].request 必须是对象")

        weight = raw.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise SuiteFormatError(f"cases[{index}].weight 必须是正数")
        if not math.isfinite(float(weight)) or float(weight) <= 0:
            raise SuiteFormatError(f"cases[{index}].weight 必须是正数")

        return cls(
            id=case_id,
            category=category,
            prompt=prompt,
            evaluator=dict(evaluator),
            request=dict(request),
            weight=float(weight),
            title=str(raw.get("title") or case_id),
            difficulty=str(raw.get("difficulty") or "未标注"),
            reference_answer=raw.get("reference_answer"),
            rationale=str(raw.get("rationale") or ""),
            critical=raw.get("critical") is True,
            negative_examples=tuple(raw.get("negative_examples") or ()),
            benchmark_refs=tuple(raw.get("benchmark_refs") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "prompt": self.prompt,
            "request": dict(self.request),
            "weight": self.weight,
            "evaluator": dict(self.evaluator),
            "title": self.title, "difficulty": self.difficulty,
            "reference_answer": self.reference_answer, "rationale": self.rationale,
            "critical": self.critical, "negative_examples": list(self.negative_examples),
            "benchmark_refs": list(self.benchmark_refs),
        }


@dataclass(frozen=True)
class QualityResult:
    """单题评分结果。score 的范围为 0 到 1。"""

    case_id: str
    category: str
    score: float
    passed: bool
    response: Any
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "score": self.score,
            "passed": self.passed,
            "response": self.response,
            "checks": list(self.checks),
            "failures": list(self.failures),
        }


@dataclass(frozen=True)
class QualityRun:
    """一次质量评测的轻量运行记录，不负责发请求。

    ``run_quality_probe`` 接收外部已经获得的响应并调用本地评分器；
    这样 UI（用户界面）可以负责资源实例和请求生命周期，本模块只负责质量判定。
    """

    id: str
    name: str
    status: str
    suite_id: str
    suite_version: int
    seed: int | None
    resources: tuple[str, ...]
    summary: "QualitySummary"
    created_at: str
    output_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "seed": self.seed,
            "resources": list(self.resources),
            "summary": self.summary.to_dict(),
            "created_at": self.created_at,
            "output_path": self.output_path,
        }


@dataclass(frozen=True)
class QualitySummary:
    """整套题的汇总结果，便于 UI（用户界面）直接展示。"""

    total_cases: int
    passed_cases: int
    score: float
    pass_rate: float
    results: tuple[QualityResult, ...]
    category_stats: Mapping[str, Mapping[str, float | int]]

    @property
    def percentage(self) -> float:
        """百分制总分。"""
        return round(self.score * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "score": self.score,
            "percentage": self.percentage,
            "pass_rate": self.pass_rate,
            "results": [result.to_dict() for result in self.results],
            "category_stats": {
                name: dict(stats) for name, stats in self.category_stats.items()
            },
        }


def _parse_suite(raw: Any) -> tuple[QualityCase, ...]:
    if not isinstance(raw, Mapping):
        raise SuiteFormatError("题集根节点必须是对象")
    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SuiteFormatError("题集 cases 必须是非空数组")

    parsed: list[QualityCase] = []
    ids: set[str] = set()
    for index, item in enumerate(cases):
        case = QualityCase.from_dict(item, index)
        if case.id in ids:
            raise SuiteFormatError(f"题目 id 重复：{case.id}")
        ids.add(case.id)
        parsed.append(case)
    return tuple(parsed)


def load_suite(path: str | Path | None = None) -> tuple[QualityCase, ...]:
    """加载 JSON（结构化数据）题集；path 为空时加载项目默认题集。"""
    suite_path = Path(path) if path is not None else _DEFAULT_SUITE_PATH
    try:
        with suite_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise SuiteFormatError(f"题集文件不存在：{suite_path}") from exc
    except json.JSONDecodeError as exc:
        raise SuiteFormatError(f"题集不是合法 JSON：{suite_path}: {exc.msg}") from exc
    except OSError as exc:
        raise SuiteFormatError(f"无法读取题集：{suite_path}: {exc}") from exc
    return _parse_suite(raw)


def suite_manifest(path=None, cases=None):
    """题集身份依据题目内容计算；创建时固定，后续文件修改不能更换试卷。"""
    suite_path = Path(path) if path else _DEFAULT_SUITE_PATH
    raw = json.loads(suite_path.read_text(encoding="utf-8"))
    case_list = tuple(cases) if cases is not None else _parse_suite(raw)
    canonical = json.dumps([c.to_dict() for c in case_list], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    counts = {}
    for case in case_list:
        counts[case.category] = counts.get(case.category, 0) + 1
    labels = {x["id"]: x.get("label", x["id"]) for x in raw.get("categories", [])}
    return {"id": raw.get("name", "custom-suite"), "version": raw.get("version", 1),
            "title": raw.get("title") or raw.get("name"), "description": raw.get("description", ""),
            "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(), "case_count": len(case_list),
            "categories": [{"id": key, "label": labels.get(key, key), "count": value} for key, value in counts.items()],
            "input_chars": {"min": min((len(c.prompt) for c in case_list), default=0), "max": max((len(c.prompt) for c in case_list), default=0), "total": sum(len(c.prompt) for c in case_list)},
            "methodology": methodology_snapshot() if raw.get("methodology_version") == 3 else None,
            "scoring": raw.get("scoring", "历史题集按题目权重汇总"),
            "limitations": raw.get("limitations", ["历史小题集只能用于冒烟观测，不能支持完整质量诊断。"])}


def default_suite() -> tuple[QualityCase, ...]:
    """返回默认确定性题集。每次调用都会重新从文件读取，避免共享可变状态。"""
    return load_suite()


def _as_text(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        for key in ("content", "text", "response", "output"):
            if key in response:
                return _as_text(response[key])
        message = response.get("message")
        if isinstance(message, Mapping):
            return _as_text(message)
        return json.dumps(response, ensure_ascii=False, sort_keys=True)
    return str(response)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _check_result(
    case: QualityCase,
    response: Any,
    checks: Iterable[tuple[str, bool]],
    *,
    threshold: float = 1.0,
) -> QualityResult:
    check_items = tuple(checks)
    check_list = tuple(name for name, _ in check_items)
    statuses = tuple(bool(ok) for _, ok in check_items)
    passed_checks = sum(statuses)
    score = passed_checks / len(statuses) if statuses else 0.0
    failures = tuple(name for name, ok in zip(check_list, statuses) if not ok)
    threshold = min(1.0, max(0.0, float(threshold)))
    return QualityResult(
        case_id=case.id,
        category=case.category,
        score=round(score, 6),
        passed=not failures and score >= threshold,
        response=response,
        checks=check_list,
        failures=failures,
    )


def _required_keywords(evaluator: Mapping[str, Any]) -> list[str]:
    keywords = evaluator.get("required_keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
        raise ValueError("required_keywords 必须是字符串数组")
    return keywords


def _score_basic(case: QualityCase, response: Any) -> QualityResult:
    evaluator = case.evaluator
    text = _as_text(response)
    case_sensitive = bool(evaluator.get("case_sensitive", False))
    haystack = text if case_sensitive else text.casefold()
    checks: list[tuple[str, bool]] = [("response_is_non_empty", bool(text.strip()))]
    min_length = evaluator.get("min_length", 1)
    if not isinstance(min_length, int) or min_length < 0:
        raise ValueError("min_length 必须是非负整数")
    checks.append(("minimum_length", len(text.strip()) >= min_length))
    for keyword in _required_keywords(evaluator):
        needle = keyword if case_sensitive else keyword.casefold()
        checks.append((f"contains:{keyword}", needle in haystack))
    return _check_result(case, response, checks, threshold=evaluator.get("pass_threshold", 1.0))


def _score_math(case: QualityCase, response: Any) -> QualityResult:
    evaluator = case.evaluator
    expected = evaluator.get("expected")
    if not isinstance(expected, (str, int, float)) or isinstance(expected, bool):
        raise ValueError("math evaluator.expected 必须是字符串或数字")
    actual = _normalize(_as_text(response))
    expected_text = _normalize(str(expected))
    allow_explanation = bool(evaluator.get("allow_explanation", False))
    if allow_explanation:
        matched = bool(re.search(rf"(?<![\w.]){re.escape(expected_text)}(?![\w.])", actual))
    else:
        matched = actual == expected_text
    return _check_result(case, response, [("exact_math_answer", matched)])


def _json_type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    raise ValueError(f"不支持的 JSON 字段类型：{expected_type}")


def _get_path(root: Any, path: str) -> tuple[bool, Any]:
    current = root
    if not path:
        return True, current
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _score_structured_json(case: QualityCase, response: Any) -> QualityResult:
    evaluator = case.evaluator
    if isinstance(response, (Mapping, list, int, float, bool)) or response is None:
        value = response
    else:
        raw_text = _as_text(response).strip()
        try:
            value = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            return _check_result(case, response, [("valid_json", False)])

    checks: list[tuple[str, bool]] = []
    root_type = evaluator.get("root_type")
    if root_type:
        checks.append((f"root_type:{root_type}", _json_type_matches(value, root_type)))
    fields = evaluator.get("fields", [])
    if not isinstance(fields, list):
        raise ValueError("structured_json evaluator.fields 必须是数组")
    for index, field_spec in enumerate(fields):
        if not isinstance(field_spec, Mapping) or not isinstance(field_spec.get("path"), str):
            raise ValueError(f"fields[{index}] 必须包含 path")
        path = field_spec["path"]
        exists, field_value = _get_path(value, path)
        checks.append((f"field_exists:{path}", exists))
        if not exists:
            continue
        expected_type = field_spec.get("type")
        if expected_type:
            checks.append((f"field_type:{path}", _json_type_matches(field_value, expected_type)))
        if "equals" in field_spec:
            checks.append((f"field_equals:{path}", field_value == field_spec["equals"]))
        if "contains" in field_spec:
            contains = field_spec["contains"]
            matched = isinstance(field_value, (str, list, tuple, set)) and contains in field_value
            checks.append((f"field_contains:{path}", matched))
    return _check_result(case, response, checks, threshold=evaluator.get("pass_threshold", 1.0))


def _score_safety_refusal(case: QualityCase, response: Any) -> QualityResult:
    evaluator = case.evaluator
    text = _as_text(response).casefold()
    refusal_keywords = evaluator.get("refusal_keywords", [])
    if not isinstance(refusal_keywords, list) or not all(isinstance(item, str) for item in refusal_keywords):
        raise ValueError("refusal_keywords 必须是字符串数组")
    forbidden_keywords = evaluator.get("forbidden_keywords", [])
    if not isinstance(forbidden_keywords, list) or not all(isinstance(item, str) for item in forbidden_keywords):
        raise ValueError("forbidden_keywords 必须是字符串数组")
    minimum = evaluator.get("min_refusal_keywords", 1)
    if not isinstance(minimum, int) or minimum < 1:
        raise ValueError("min_refusal_keywords 必须是正整数")
    refusal_hits = sum(1 for item in refusal_keywords if item.casefold() in text)
    checks = [
        ("has_refusal_language", refusal_hits >= minimum),
        ("contains_no_forbidden_instructions", not any(item.casefold() in text for item in forbidden_keywords)),
    ]
    return _check_result(case, response, checks, threshold=evaluator.get("pass_threshold", 1.0))


def _score_long_text(case: QualityCase, response: Any) -> QualityResult:
    evaluator = case.evaluator
    text = _as_text(response)
    checks: list[tuple[str, bool]] = []
    minimum = evaluator.get("min_length", 1)
    if not isinstance(minimum, int) or minimum < 0:
        raise ValueError("min_length 必须是非负整数")
    checks.append(("minimum_length", len(text.strip()) >= minimum))
    for keyword in _required_keywords(evaluator):
        checks.append((f"contains:{keyword}", keyword in text))
    rules = evaluator.get("consistency_rules", [])
    if not isinstance(rules, list):
        raise ValueError("consistency_rules 必须是数组")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or not isinstance(rule.get("pattern"), str):
            raise ValueError(f"consistency_rules[{index}] 必须包含 pattern")
        minimum_occurrences = rule.get("min_occurrences", 2)
        if not isinstance(minimum_occurrences, int) or minimum_occurrences < 1:
            raise ValueError(f"consistency_rules[{index}].min_occurrences 必须是正整数")
        try:
            occurrences = len(re.findall(rule["pattern"], text))
        except re.error as exc:
            raise ValueError(f"consistency_rules[{index}].pattern 不是合法正则：{exc}") from exc
        checks.append((f"pattern_occurrences:{index}", occurrences >= minimum_occurrences))
    return _check_result(case, response, checks, threshold=evaluator.get("pass_threshold", 1.0))


def _strict_json_equal(actual, expected):
    # bool 是 Python int 的子类；结构化评测不能把 true 当作 1。
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_strict_json_equal(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_strict_json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _score_exact(case, response):
    expected = unicodedata.normalize("NFC", str(case.evaluator["expected"]))
    actual = unicodedata.normalize("NFC", _as_text(response).strip())
    return _check_result(case, response, [("答案及指定格式完全匹配", actual == expected)])


def _score_json_exact(case, response):
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("重复字段")
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError("非标准数字")
    try:
        actual = json.loads(_as_text(response), object_pairs_hook=no_duplicates, parse_constant=invalid_constant)
    except (ValueError, TypeError):
        return _check_result(case, response, [("合法结构化数据（无重复键、代码块或额外正文）", False)])
    # 合法 JSON 本身不是内容质量，不能仅因为语法正确就拿到一半分数。
    return _check_result(case, response, [("字段、类型、值与数组顺序完全匹配", _strict_json_equal(actual, case.evaluator["expected"]))])


def _score_constraints(case, response):
    """原创、可机械验证的约束；辅助逐项反馈，不用参考作文全文匹配。"""
    text = unicodedata.normalize("NFC", _as_text(response).strip())
    rules = case.evaluator.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("约束规则必须为非空数组")
    checks = []
    for rule in rules:
        kind = rule["kind"]
        if kind == "contains":
            ok = all(word in text for word in rule["values"])
        elif kind == "excludes":
            ok = all(word not in text for word in rule["values"])
        elif kind == "starts_with":
            ok = text.startswith(rule["value"])
        elif kind == "ends_with":
            ok = text.endswith(rule["value"])
        elif kind == "line_count":
            ok = len(text.splitlines()) == rule["value"] and all(line.strip() for line in text.splitlines())
        elif kind == "char_range":
            ok = rule["min"] <= len(text) <= rule["max"]
        elif kind == "occurrences":
            ok = text.count(rule["value"]) == rule["count"]
        elif kind == "fullmatch":
            ok = re.fullmatch(rule["pattern"], text) is not None
        elif kind == "each_line_prefix":
            lines = text.splitlines()
            ok = bool(lines) and all(line.startswith(rule["value"]) for line in lines)
        else:
            raise ValueError("未知约束类型：" + str(kind))
        checks.append((rule["label"], ok))
    # 题分与约束通过数分开：满足部分约束不能通过该题。
    return QualityResult(case_id=case.id, category=case.category, score=1.0 if all(ok for _, ok in checks) else 0.0,
                         passed=all(ok for _, ok in checks), response=response,
                         checks=tuple(label for label, _ in checks), failures=tuple(label for label, ok in checks if not ok))


def score_case(case: QualityCase, response: Any) -> QualityResult:
    """对单条题目评分；评分配置或响应异常会变成可定位的失败结果。"""
    try:
        evaluator_type = case.evaluator.get("type")
        scorers = {
            "exact_text": _score_exact,
            "verifiable_constraints": _score_constraints,
            "json_exact": _score_json_exact,
            "basic_response": _score_basic,
            "math": _score_math,
            "structured_json": _score_structured_json,
            "safety_refusal": _score_safety_refusal,
            "long_text_consistency": _score_long_text,
        }
        scorer = scorers.get(evaluator_type)
        if scorer is None:
            raise ValueError(f"不支持的 evaluator.type：{evaluator_type}")
        return scorer(case, response)
    except Exception as exc:  # 评分入口对调用方保持结构化返回。
        return QualityResult(
            case_id=case.id,
            category=case.category,
            score=0.0,
            passed=False,
            response=response,
            failures=(f"评分异常：{exc}",),
        )


def _response_map(cases: Sequence[QualityCase], responses: Mapping[str, Any] | Sequence[Any]) -> dict[str, Any]:
    if isinstance(responses, Mapping):
        return dict(responses)
    values = list(responses)
    if len(values) != len(cases):
        raise ValueError("响应数组长度必须与题目数量一致")
    return {case.id: value for case, value in zip(cases, values)}


def score_suite(
    cases: Iterable[QualityCase],
    responses: Mapping[str, Any] | Sequence[Any],
) -> QualitySummary:
    """按题目顺序评分；responses 可按题目 id 提供，或按题目顺序提供。"""
    case_list = tuple(cases)
    response_map = _response_map(case_list, responses)
    results = tuple(
        score_case(case, response_map[case.id]) if case.id in response_map else QualityResult(
            case_id=case.id,
            category=case.category,
            score=0.0,
            passed=False,
            response=None,
            failures=("缺少该题响应",),
        )
        for case in case_list
    )
    total_weight = sum(case.weight for case in case_list)
    weighted_score = (
        sum(case.weight * result.score for case, result in zip(case_list, results)) / total_weight
        if total_weight
        else 0.0
    )
    passed_cases = sum(result.passed for result in results)
    category_accumulator: dict[str, dict[str, float | int]] = {}
    for case, result in zip(case_list, results):
        stats = category_accumulator.setdefault(
            case.category,
            {"total": 0, "passed": 0, "score_sum": 0.0, "weight": 0.0},
        )
        stats["total"] = int(stats["total"]) + 1
        stats["passed"] = int(stats["passed"]) + int(result.passed)
        stats["score_sum"] = float(stats["score_sum"]) + result.score * case.weight
        stats["weight"] = float(stats["weight"]) + case.weight

    category_stats: dict[str, dict[str, float | int]] = {}
    for category, stats in category_accumulator.items():
        weight = float(stats["weight"])
        total = int(stats["total"])
        category_stats[category] = {
            "total": total,
            "passed": int(stats["passed"]),
            "score": round(float(stats["score_sum"]) / weight, 6) if weight else 0.0,
            "pass_rate": int(stats["passed"]) / total if total else 0.0,
        }

    return QualitySummary(
        total_cases=len(case_list),
        passed_cases=passed_cases,
        score=round(weighted_score, 6),
        pass_rate=passed_cases / len(case_list) if case_list else 0.0,
        results=results,
        category_stats=category_stats,
    )


def run_quality_probe(
    responses: Mapping[str, Any] | Sequence[Any],
    cases: Iterable[QualityCase] | None = None,
    *,
    name: str = "资源实例质量测试",
    run_id: str | None = None,
    seed: int | None = None,
    resources: Iterable[str] = (),
    output_path: str | None = None,
) -> QualityRun:
    """对外部已取得的响应执行一次本地质量评测。

    此函数刻意不接收 URL、API Key（应用程序编程接口密钥）或 HTTP（超文本传输协议）
    客户端，因此不会产生网络副作用。``responses`` 可以是按题目 id 索引的映射，
    也可以是与题集顺序一致的响应序列。
    """
    case_list = tuple(cases) if cases is not None else default_suite()
    summary = score_suite(case_list, responses)
    normalized_resources = tuple(str(item) for item in resources)
    return QualityRun(
        id=run_id or uuid.uuid4().hex[:12],
        name=str(name),
        status="finished",
        suite_id="custom-snapshot" if cases is not None else "resource-quality-120",
        suite_version=2 if cases is None else 1,
        seed=seed,
        resources=normalized_resources,
        summary=summary,
        created_at=singapore_timestamp(datetime.now(SINGAPORE_TIMEZONE)),
        output_path=output_path,
    )


__all__ = [
    "QualityCase",
    "QualityRun",
    "QualityResult",
    "QualitySummary",
    "SuiteFormatError",
    "default_suite",
    "load_suite",
    "score_case",
    "score_suite",
    "run_quality_probe",
]
