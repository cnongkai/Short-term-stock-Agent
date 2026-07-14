"""
LLM 客户端抽象基类 (对应 PRD Phase 0)

所有 LLM 客户端需实现 get_llm() 方法返回可绑工具的 langchain LLM 实例。
参考架构: TradingAgents-CN llm_clients/base_client.py
"""
from abc import ABC, abstractmethod
from typing import Any


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类"""

    def __init__(self, model: str, base_url: str = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    @abstractmethod
    def get_llm(self) -> Any:
        """返回 langchain 兼容的 LLM 实例 (支持 bind_tools)"""
        raise NotImplementedError
