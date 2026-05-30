"""
stream_usage_chat_openai.py
─────────────────────────────────────────────
LangChain ChatOpenAI 的串流使用量補丁。

問題：舊版 langchain-openai 在 stream_options.include_usage=true 時，OpenAI
最後會送出一個 choices=[] 的 chunk 附帶 usage，但原始實作直接略過這包，
導致 token 用量永遠無法從串流事件中取得。

解法：繼承 ChatOpenAI 並覆寫 _astream，補上對 choices 為空時的 usage 解析，
將其包成帶 response_metadata["token_usage"] 的 AIMessageChunk 向上傳遞。

用途：被整個專案所有需要計費的模型（Router、Analyst、Flash Analyst、Rewrite）
      共同繼承，是唯一的 LLM 客戶端類別。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import base as lc_openai_base


def _openai_usage_to_token_usage(raw: Any) -> Optional[Dict[str, int]]:
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump()
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        p = raw.get("prompt_tokens")
        if p is None:
            p = raw.get("input_tokens")
        c = raw.get("completion_tokens")
        if c is None:
            c = raw.get("output_tokens")
        p_i = int(p or 0)
        c_i = int(c or 0)
    except (TypeError, ValueError):
        return None
    if p_i == 0 and c_i == 0:
        return None
    total = raw.get("total_tokens")
    try:
        t_i = int(total) if total is not None else p_i + c_i
    except (TypeError, ValueError):
        t_i = p_i + c_i
    return {
        "prompt_tokens": p_i,
        "completion_tokens": c_i,
        "total_tokens": t_i,
    }


class StreamUsageChatOpenAI(ChatOpenAI):
    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message_dicts, params = self._create_message_dicts(messages, stop)
        params = {**params, **kwargs, "stream": True}

        default_chunk_class = AIMessageChunk
        async for chunk in await self.async_client.create(
            messages=message_dicts,
            **params,
        ):
            if not isinstance(chunk, dict):
                chunk = chunk.model_dump()
            choices = chunk.get("choices") or []
            if len(choices) == 0:
                tu_dict = _openai_usage_to_token_usage(chunk.get("usage"))
                if tu_dict is not None:
                    rmeta: Dict[str, Any] = {"token_usage": tu_dict}
                    if mn := chunk.get("model"):
                        rmeta["model_name"] = mn
                    meta_chunk = AIMessageChunk(content="", response_metadata=rmeta)
                    cg = ChatGenerationChunk(message=meta_chunk)
                    if run_manager:
                        await run_manager.on_llm_new_token(
                            token=cg.text, chunk=cg, logprobs=None
                        )
                    yield cg
                continue

            choice = choices[0]
            msg_chunk = lc_openai_base._convert_delta_to_message_chunk(
                choice["delta"], default_chunk_class
            )
            generation_info: Dict[str, Any] = {}
            if finish_reason := choice.get("finish_reason"):
                generation_info["finish_reason"] = finish_reason
            logprobs = choice.get("logprobs")
            if logprobs:
                generation_info["logprobs"] = logprobs
            default_chunk_class = msg_chunk.__class__
            cg = ChatGenerationChunk(
                message=msg_chunk, generation_info=generation_info or None
            )
            if run_manager:
                await run_manager.on_llm_new_token(
                    token=cg.text, chunk=cg, logprobs=logprobs
                )
            yield cg
