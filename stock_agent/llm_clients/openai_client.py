"""
OpenAI 兼容 LLM 客户端 (对应 PRD Phase 0)

支持 DeepSeek/智谱GLM/通义千问 等所有 OpenAI 兼容协议的 LLM。
返回 langchain_openai.ChatOpenAI 实例, 支持 bind_tools 工具调用。
参考架构: TradingAgents-CN llm_clients/openai_client.py
"""
from typing import Any

from stock_agent.llm_clients.base_client import BaseLLMClient


class OpenAIClient(BaseLLMClient):
    """OpenAI 兼容协议 LLM 客户端

    适用于 DeepSeek (deepseek-chat)、智谱 GLM (glm-4-plus)、
    通义千问 (qwen-plus) 等所有兼容 OpenAI API 的模型。
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        provider: str = "deepseek",
        temperature: float = 0.7,
        max_tokens: int = 4000,
        timeout: int = 180,
        **extra_kwargs,
    ):
        super().__init__(model, base_url, **extra_kwargs)
        self.api_key = api_key
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def get_llm(self) -> Any:
        """返回 ChatOpenAI 实例 (支持 bind_tools)"""
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            **self.kwargs,
        )
