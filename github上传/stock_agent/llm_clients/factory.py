"""
LLM 客户端工厂 (对应 PRD Phase 0)

根据 provider 创建对应的 LLM 客户端实例。
DeepSeek/智谱GLM/通义千问 等均走 OpenAI 兼容协议。
参考架构: TradingAgents-CN llm_clients/factory.py
"""
import os
from typing import Optional

from stock_agent.llm_clients.base_client import BaseLLMClient

# provider 别名归一化
_PROVIDER_ALIASES = {
    "dashscope": "qwen",
    "alibaba": "qwen",
    "zhipu": "glm",
    "siliconflow": "openai",
}

# 走 OpenAI 兼容协议的 provider 集合
_OPENAI_COMPATIBLE = {
    "openai",
    "deepseek",
    "qwen",
    "glm",
    "qianfan",
    "openrouter",
    "aihubmix",
    "ollama",
    "custom_openai",
}

# provider → (默认 base_url, API Key 环境变量名)
_PROVIDER_CONFIG = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


def normalize_provider_key(provider: str) -> str:
    """归一化 provider 名称"""
    if not provider:
        return "deepseek"
    p = provider.lower().strip()
    return _PROVIDER_ALIASES.get(p, p)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """创建 LLM 客户端 (统一入口)

    Args:
        provider: 供应商 (deepseek/glm/qwen/openai 等)
        model: 模型名称
        base_url: API 地址 (None 则用 provider 默认)
        api_key: API Key (None 则从环境变量读取)
        **kwargs: 其他参数 (temperature/max_tokens/timeout 等)

    Returns:
        BaseLLMClient 实例
    """
    provider_lower = normalize_provider_key(provider)

    if provider_lower in _OPENAI_COMPATIBLE:
        from stock_agent.llm_clients.openai_client import OpenAIClient

        # 解析默认 base_url 和 api_key
        default_url, env_key = _PROVIDER_CONFIG.get(
            provider_lower, (None, f"{provider_lower.upper()}_API_KEY")
        )
        final_url = base_url or default_url
        final_key = api_key or os.getenv(env_key, "")

        if not final_key:
            raise ValueError(
                f"使用 {provider_lower} 需要在 .env 中设置 {env_key} 环境变量"
            )

        return OpenAIClient(
            model=model,
            base_url=final_url,
            api_key=final_key,
            provider=provider_lower,
            **kwargs,
        )

    raise ValueError(f"不支持的 LLM provider: {provider}")
