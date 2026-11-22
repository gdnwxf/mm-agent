"""LLM 抽象层：把「模型调用」与「模式逻辑」解耦。

模式代码只依赖 :class:`LLM`，因此同一份 Agent 代码可以跑在两种实现上：

- :class:`ScriptedLLM`：规则驱动的确定性假模型，默认使用。它不推理、不理解语义，
  只负责让七种模式的循环在离线下可跑、可复现、可断言，便于观察每一步的输入输出。
- :class:`OpenAICompatLLM`：OpenAI 兼容的 ``/chat/completions`` 真实模型，
  用 ``MM_AGENT_LLM=openai`` 或 ``--llm openai`` 启用。

注意：假模型不代表真实模型的行为，只代表「模型按协议返回了这样的文本」。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence


class LLMError(RuntimeError):
    """模型调用失败：网络不可达、鉴权失败、响应格式不符合预期等。"""


@dataclass(frozen=True)
class Message:
    """一条对话消息；``role`` 取 system / user / assistant / tool。"""

    role: str
    content: str

    def render(self) -> str:
        return f"[{self.role}]\n{self.content}"


def render_messages(messages: Sequence[Message]) -> str:
    """把消息序列渲染成单一提示词。

    :class:`ScriptedLLM` 的规则匹配的就是这段文本，因此渲染格式属于内核契约：
    规则里引用的标记（如 ``Observation:`` 的行）必须能在提示词中原样出现。
    """

    return "\n\n".join(message.render() for message in messages)


class LLM(ABC):
    """模型接口。

    子类只实现 :meth:`_generate`；调用计数与历史记录由基类统一维护，
    方便上层统计「这个模式花了多少次模型调用」。
    """

    def __init__(self, label: str = "llm") -> None:
        self.label = label
        self.calls = 0
        self.history: list[dict[str, Any]] = []

    def chat(self, messages: Sequence[Message], *, stop: Sequence[str] | None = None) -> str:
        prompt = render_messages(messages)
        self.calls += 1
        reply = self._generate(prompt, messages, stop=stop)
        self.history.append({"call": self.calls, "prompt_chars": len(prompt), "reply": reply})
        return reply

    def ask(self, prompt: str, *, system: str = "") -> str:
        """单轮便捷调用：可选 system 提示 + 一条 user 消息。"""

        messages = [Message("system", system)] if system else []
        messages.append(Message("user", prompt))
        return self.chat(messages)

    @abstractmethod
    def _generate(self, prompt: str, messages: Sequence[Message], *, stop: Sequence[str] | None) -> str:
        """由子类实现的实际生成逻辑。"""


@dataclass(frozen=True)
class Rule:
    """ScriptedLLM 的一条规则。

    ``pattern`` 是正则，作用于渲染后的整段提示词；首条命中的规则决定回复。
    ``reply`` 为字符串时原样返回（不做格式化，避免 JSON 花括号被误解析），
    为可调用对象时接收 ``(匹配对象, 提示词)``，可用于从提示词里抽取事实后拼装回复。
    """

    name: str
    pattern: str
    reply: str | Callable[[re.Match[str], str], str]
    flags: int = re.IGNORECASE | re.MULTILINE


class ScriptedLLM(LLM):
    """规则驱动的确定性假模型，用于离线复现模式循环。

    它按注册顺序匹配提示词，命中规则即返回。因此示例的提示词模板必须保留规则
    依赖的标记（例如累积的 ``Observation:`` 内容），规则才能表现出「模型看到了
    上一步结果后改变下一步动作」的效果。
    """

    def __init__(
        self,
        rules: Iterable[Rule] = (),
        *,
        fallback: str = "无法生成回复：ScriptedLLM 没有命中任何规则。",
        label: str = "scripted",
    ) -> None:
        super().__init__(label)
        self.rules = tuple(rules)
        self.fallback = fallback
        self.matched_rules: list[str] = []

    def _generate(self, prompt: str, messages: Sequence[Message], *, stop: Sequence[str] | None) -> str:
        for rule in self.rules:
            match = re.search(rule.pattern, prompt, rule.flags)
            if match is None:
                continue
            self.matched_rules.append(rule.name)
            if callable(rule.reply):
                return rule.reply(match, prompt)
            return rule.reply
        self.matched_rules.append("<fallback>")
        return self.fallback


class OpenAICompatLLM(LLM):
    """OpenAI 兼容接口客户端。

    只用标准库实现，避免示例集引入第三方依赖；``api_key`` 不进入任何日志或 Trace。
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        super().__init__(label=f"openai:{model}")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.temperature = temperature

    def _generate(self, prompt: str, messages: Sequence[Message], *, stop: Sequence[str] | None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": self.temperature,
        }
        if stop:
            payload["stop"] = list(stop)
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMError(f"模型接口返回 {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"模型接口不可达：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型响应不是合法 JSON：{exc}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型响应缺少 choices[0].message.content：{body}") from exc
        return content or ""


def build_llm(
    kind: str | None = None,
    *,
    rules: Iterable[Rule] = (),
    fallback: str = "",
    label: str = "scripted",
) -> LLM:
    """按类型构造模型。

    类型优先级：显式入参 > 环境变量 ``MM_AGENT_LLM`` > ``scripted``。
    真实模型所需环境变量：``MM_AGENT_API_KEY``（或 ``OPENAI_API_KEY``）、
    可选 ``MM_AGENT_MODEL``、``MM_AGENT_BASE_URL``。
    """

    resolved = (kind or os.environ.get("MM_AGENT_LLM") or "scripted").strip().lower()
    if resolved in {"scripted", "mock", "offline"}:
        kwargs: dict[str, Any] = {"label": label}
        if fallback:
            kwargs["fallback"] = fallback
        return ScriptedLLM(rules, **kwargs)
    if resolved in {"openai", "openai-compat", "real"}:
        api_key = os.environ.get("MM_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("使用真实模型需要设置 MM_AGENT_API_KEY 或 OPENAI_API_KEY。")
        return OpenAICompatLLM(
            os.environ.get("MM_AGENT_MODEL", "gpt-4o-mini"),
            api_key=api_key,
            base_url=os.environ.get("MM_AGENT_BASE_URL", "https://api.openai.com/v1"),
        )
    raise LLMError(f"未知的 LLM 类型：{resolved}（可选 scripted / openai）")
