"""
深度研究：把 Agents SDK 的 token 用量接進既有的配額／計費模組。

與聊天流程的差異只有兩點，其餘（扣配額、寫流水帳、估價）完全共用：

- **沒有 chat_id**：研究不隸屬任何一則對話，`token_usage_logs.chat_id` 留 NULL
  （欄位本身可為 NULL），改以 `caller` 分辨來源：
  `deep_research`（研究本身）、`deep_research_report` / `deep_research_deck`（產出檔案）。
- **用量來自 Agents SDK 而非 LangChain**：`Runner` 把每次模型呼叫的 usage 累加在
  `result.context_wrapper.usage`，所以一次研究（web search 會跑很多輪）只結算一次。

`context_wrapper.usage` 在串流途中就會持續累加，因此即使使用者中途關掉頁面，
仍能把「已經燒掉的」token 記進配額 —— 那些 token OpenAI 一樣會收錢。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from app.backend.module.token_usage import record_token_usage

# token_usage_logs.caller（VARCHAR(50)）：用來把深度研究的花費從聊天中分離出來
CALLER_RESEARCH = "deep_research"
_CALLER_BY_KIND = {
    "report": "deep_research_report",
    "deck": "deep_research_deck",
}


def caller_for_artifact(kind: str) -> str:
    return _CALLER_BY_KIND.get(kind, f"deep_research_{kind}"[:50])


@dataclass
class AgentUsage:
    """一次 `Runner` 執行（可能含多輪模型呼叫）的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
        }

    def add(self, other: "AgentUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.requests += other.requests

    def absorb(self, run_result: Any) -> "AgentUsage":
        """
        從 `Runner.run()` / `Runner.run_streamed()` 的結果讀出目前累計用量。

        是覆寫而非累加：`context_wrapper.usage` 本身已經是這次執行的總和，
        累加會讓串流途中呼叫兩次的人得到雙倍數字。
        取不到（SDK 換版、模型沒回 usage）時保持原值，不要讓計費把流程弄掛。
        """
        usage = getattr(getattr(run_result, "context_wrapper", None), "usage", None)
        if usage is None:
            return self
        self.prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        self.completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.requests = int(getattr(usage, "requests", 0) or 0)
        return self


async def record_agent_usage(
    *,
    user_id: UUID,
    model: str,
    usage: Optional[AgentUsage],
    caller: str,
    session_id: Optional[str] = None,
) -> None:
    """
    把一次 Agents SDK 執行的用量寫進 `user_usage_quotas` 與 `token_usage_logs`。

    沒有用量（模型沒回 usage、或還沒送出任何請求就失敗）時直接跳過。
    `record_token_usage` 內部已吞掉所有 DB 例外，計費失敗不會影響已完成的研究。
    """
    if usage is None or usage.total_tokens <= 0:
        return

    print(
        f"[DEEP-SEARCH] usage caller={caller} session={session_id} model={model} "
        f"requests={usage.requests} prompt={usage.prompt_tokens} "
        f"completion={usage.completion_tokens}",
        flush=True,
    )
    await record_token_usage(
        user_id=user_id,
        chat_id=None,
        message_id=None,
        model_name=model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        caller=caller,
    )


__all__ = [
    "AgentUsage",
    "CALLER_RESEARCH",
    "caller_for_artifact",
    "record_agent_usage",
]
