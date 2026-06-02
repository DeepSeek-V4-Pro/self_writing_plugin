"""统一 LLM 客户端 —— 支持内置 ctx.llm 和自定义 OpenAI 兼容 API。"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol


class LlmResponse:
    __slots__ = ("success", "response", "reasoning", "error")

    def __init__(
        self,
        success: bool = False,
        response: str = "",
        reasoning: str = "",
        error: str = "",
    ):
        self.success = success
        self.response = response
        self.reasoning = reasoning
        self.error = error


class LlmClient(Protocol):
    async def generate(self, prompt: str, timeout: int, model: str = "") -> LlmResponse:
        ...


class CtxLlmClient:
    """使用 MaiBot 内置 ctx.llm.generate() 的客户端。"""

    def __init__(self, ctx: Any):
        self._ctx = ctx

    async def generate(self, prompt: str, timeout: int, model: str = "") -> LlmResponse:
        kwargs: dict[str, Any] = {"prompt": prompt, "model": model}

        try:
            result = await asyncio.wait_for(
                self._ctx.llm.generate(**kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return LlmResponse(error="LLM 调用超时")
        except Exception as e:
            return LlmResponse(error=f"LLM 调用异常: {e}")

        if not isinstance(result, dict) or not result.get("success", False):
            msg = result.get("error", "未知错误") if isinstance(result, dict) else "返回格式异常"
            return LlmResponse(error=f"LLM 返回失败: {msg}")

        return LlmResponse(
            success=True,
            response=str(result.get("response", "")),
            reasoning=str(result.get("reasoning", "")),
        )


class OpenAiCompatClient:
    """OpenAI 兼容 API 客户端 (aiohttp)。"""

    _HEADERS_TEMPLATE = {
        "Content-Type": "application/json",
    }

    def __init__(self, api_url: str, api_key: str, default_model: str = ""):
        self._api_url = api_url.rstrip("/") + "/chat/completions"
        self._api_key_masked = _mask_key(api_key)
        self._default_model = default_model
        self._headers = dict(self._HEADERS_TEMPLATE)
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def generate(self, prompt: str, timeout: int, model: str = "") -> LlmResponse:
        selected_model = model or self._default_model
        if not selected_model:
            return LlmResponse(error="未指定模型名称 (model)")

        payload = {
            "model": selected_model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 8192,
        }

        async def _call():
            import aiohttp
            async with aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                async with session.post(
                    self._api_url,
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return LlmResponse(
                            error=f"API 返回 HTTP {resp.status}: {body[:300]}"
                        )
                    data = await resp.json()
                    return data

        try:
            result = await asyncio.wait_for(_call(), timeout=timeout + 5)
        except asyncio.TimeoutError:
            return LlmResponse(error="自定义 API 调用超时")
        except ImportError:
            return LlmResponse(error="aiohttp 未安装，无法使用自定义 API")
        except Exception as e:
            return LlmResponse(error=f"自定义 API 调用异常: {e}")

        if isinstance(result, LlmResponse):
            return result

        try:
            data = result
            choices = data.get("choices", [])
            if not choices:
                return LlmResponse(error="API 返回空响应")
            content = str(choices[0].get("message", {}).get("content", ""))
            if not content.strip():
                return LlmResponse(error="API 返回空内容")
            return LlmResponse(
                success=True,
                response=content,
                reasoning="",
            )
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return LlmResponse(error=f"API 响应解析失败: {e}")


def create_llm_client(
    ctx: Any,
    custom_api_enabled: bool = False,
    custom_api_url: str = "",
    custom_api_key: str = "",
    custom_api_model: str = "",
) -> LlmClient:
    if custom_api_enabled and custom_api_url:
        return OpenAiCompatClient(custom_api_url, custom_api_key, custom_api_model)
    return CtxLlmClient(ctx)


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]
