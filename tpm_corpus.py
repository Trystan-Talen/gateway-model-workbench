"""可重复合成语料：预生成长前缀，未知分词器通过付费校准实测调整。

没有对应本地分词器时，synthetic_units 是合成词单元数量，绝不是 Token 计数。
初次小样本探测后根据实测完整输入比例调整实际文本，再由后续请求验证。
"""
from __future__ import annotations

import hashlib
import json
import math
import random

_WORDS = ("river mountain forest cloud stone valley garden ocean light shadow summer winter "
          "spring autumn field meadow island bridge north south east west green blue amber silver "
          "quiet clear gentle bright warm cool soft smooth stable simple common rare small large "
          "water earth wind rain snow leaf branch flower grass seed root fruit bird fish deer fox "
          "book page paper pencil table window room house road path town village market school "
          "circle square line point shape space time day night morning evening noon season "
          "record sample group measure compare count observe review explain summary result "
          "alpha beta gamma delta copper bronze cotton wool linen glass wood clay sand coral "
          "harbor station cabin tower farm lake stream hill ridge plain coast shore bay cave").split()


def _preloaded_tokenizer(model):
    """复用已加载的对应分词器，不在离线预览时触发字典下载或任何网络访问。"""
    try:
        import tiktoken
        from tiktoken.registry import ENCODINGS
        name = tiktoken.encoding_name_for_model(model)
        return ENCODINGS.get(name)
    except (ImportError, KeyError, AttributeError):
        return None


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class Corpus:
    def __init__(self, config, tokenizer=None):
        # 语料对象贯穿整轮任务，不复制凭据或连接绑定，避免结束后遗留密钥副本。
        self.config = {key: config[key] for key in ("input_tokens", "model", "seed", "template_groups",
                       "protocol_overhead_tokens", "cache_mode", "output_limit_parameter", "max_output_tokens")}
        self.target = int(config["input_tokens"])
        self.tokenizer = tokenizer or _preloaded_tokenizer(config.get("model", ""))
        self.units = min(2048, max(64, self.target))
        self.prefixes = []
        self._encoded_prefixes = ()
        self._payload_head = self._payload_tail = b""
        self.local_input_tokens = None
        self.observed_input_tokens = None
        self.input_verified = False
        self.adjustments = 0
        self.source = "local_tokenizer_content_estimate" if self.tokenizer else "unmeasured_synthetic_probe"
        self._body_bytes = 0
        self._rebuild()
        if self.tokenizer:
            self._fit_local()

    def _seed(self, group):
        return int.from_bytes(hashlib.sha256(f"{self.config['seed']}:{group}".encode()).digest()[:8], "big")

    def _rebuild(self):
        if self.units * int(self.config["template_groups"]) > 16_000_000:
            raise ValueError("校准比例异常或语料规模超过本机上限，请核对用量语义并减少模板组数")
        prefixes = []
        for group in range(int(self.config["template_groups"])):
            rng = random.Random(self._seed(group))
            # 每组材料独立且可复现，组内长前缀逐字稳定。
            words = [_WORDS[rng.randrange(len(_WORDS))] for _ in range(self.units)]
            lines = [" ".join(words[index:index + 24]) for index in range(0, len(words), 24)]
            prefixes.append(f"合成材料组 {group:04d}。以下是人工生成的无个人信息材料，请完整阅读。\n" + "\n".join(lines))
        self.prefixes = prefixes
        self.fingerprint = hashlib.sha256("\n\0\n".join(prefixes).encode()).hexdigest()
        if self.tokenizer:
            self.local_input_tokens = self._local_count()
        self._prepare_payload()
        self._body_bytes = len(self.request_bytes("metadata-request", 0))

    def _prepare_payload(self):
        """Cache escaped string fragments, never a request or a placeholder substitution.

        Derive the small envelope from the existing body builder so model, message
        role, streaming options and the selected output limit share one source.
        Each escaped fragment excludes only its enclosing JSON quotes; concatenating
        fragments therefore preserves the exact encoding of the complete content.
        """
        frame = self.request_body("", 0)
        message = frame.pop("messages")[0]
        message.pop("content")
        model = frame.pop("model")
        self._payload_head = (b'{"model":' + _json_bytes(model) + b',"messages":['
                              + _json_bytes(message)[:-1] + b',"content":"')
        self._payload_tail = b'"}],' + _json_bytes(frame)[1:]
        self._encoded_prefixes = tuple(_json_bytes(prefix)[1:-1] for prefix in self.prefixes)

    def _local_count(self):
        # 内容分词可精确；消息封装开销依接口而异，因此仍标为估算并由 usage 核验。
        content = "\n".join(message["content"] for message in self.messages("local-tokenizer-probe", 0))
        return len(self.tokenizer.encode(content)) + self.config["protocol_overhead_tokens"]

    def _fit_local(self):
        for _ in range(5):
            observed = self._local_count()
            if abs(observed - self.target) <= max(8, self.target * .005):
                break
            self.units = max(1, min(2_000_000, round(self.units * self.target / max(1, observed))))
            self._rebuild()
        self.local_input_tokens = self._local_count()

    def _message_parts(self, request_id, group):
        group = int(group) % len(self.prefixes)
        early = ""
        if self.config["cache_mode"] == "avoid_cache":
            marker = hashlib.sha256(f"{self.config['seed']}:{request_id}:{group}".encode()).hexdigest()
            early = f"本次独立材料标记 {marker}。\n"
        question = f"\n材料已结束。请求编号：{request_id}。请用约 200～300 字简短概括材料结构，不要逐项复述。"
        return group, early, question

    def messages(self, request_id, group=0):
        group, early, question = self._message_parts(request_id, group)
        return [{"role": "user", "content": early + self.prefixes[group] + question}]

    def request_body(self, request_id, group=0):
        return {"model": self.config.get("model", ""), "messages": self.messages(request_id, group),
                "stream": True, "stream_options": {"include_usage": True},
                self.config["output_limit_parameter"]: self.config["max_output_tokens"]}

    def request_bytes(self, request_id, group=0):
        """Encode only the short request-specific fragments, then assemble one body."""
        group, early, question = self._message_parts(request_id, group)
        return b"".join((self._payload_head,
                         _json_bytes(early)[1:-1] if early else b"",
                         self._encoded_prefixes[group], _json_bytes(question)[1:-1],
                         self._payload_tail))

    def resize_from_usage(self, observed_input):
        """按实测完整输入调整真正发送的文本；返回是否改变前缀（改变后须重新预热）。"""
        if isinstance(observed_input, bool) or not isinstance(observed_input, (int, float)) or not math.isfinite(observed_input) or observed_input <= 0:
            raise ValueError("调整语料需要有效的上游完整输入用量")
        self.observed_input_tokens = float(observed_input)
        self.input_verified = abs(observed_input - self.target) / self.target <= .01
        self.source = "upstream_usage_calibrated"
        if self.input_verified:
            return False
        previous = self.units
        self.units = max(1, min(2_000_000, round(self.units * self.target / observed_input)))
        if self.units == previous:
            return False
        self.adjustments += 1
        self._rebuild()
        return True

    def metadata(self):
        return {"seed": self.config["seed"], "fingerprint": self.fingerprint,
                "target_input_tokens": self.target, "synthetic_units": self.units,
                "local_input_tokens": self.local_input_tokens, "observed_input_tokens": self.observed_input_tokens,
                "input_verified": self.input_verified, "source": self.source, "body_bytes": self._body_bytes,
                "request_body_bytes": self._body_bytes, "template_groups": len(self.prefixes), "adjustments": self.adjustments,
                "note": "初始词单元不是词元数；实际长输入按上游完整用量校准，分词器内容计数不含准确协议开销"}
