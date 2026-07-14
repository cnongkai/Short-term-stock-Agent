"""
LLM 客户端模块 (对应 PRD Phase 0 LLM 配置)

提供统一的 LLM 创建入口, 支持 OpenAI 兼容协议 (DeepSeek/智谱GLM/通义千问等)。
参考架构: TradingAgents-CN llm_clients/
"""
from stock_agent.llm_clients.factory import create_llm_client
from stock_agent.llm_clients.base_client import BaseLLMClient

__all__ = ["create_llm_client", "BaseLLMClient"]
