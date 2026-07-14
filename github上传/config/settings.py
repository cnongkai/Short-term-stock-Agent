"""
配置加载器 (对应 PRD Phase 0 用户配置 / M9 用户配置面板)

从 .env 加载环境变量, 与 DEFAULT_CONFIG 合并, 供系统各模块使用。
参考架构: TradingAgents-CN config 模块
"""
import os
from typing import Dict, Any

from dotenv import load_dotenv

from stock_agent.default_config import DEFAULT_CONFIG


def load_config(env_path: str = None) -> Dict[str, Any]:
    """加载配置: .env 环境变量覆盖 DEFAULT_CONFIG

    Args:
        env_path: .env 文件路径, 默认为项目根目录下的 .env

    Returns:
        合并后的配置字典
    """
    if env_path is None:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        env_path = os.path.abspath(env_path)

    # 加载 .env 文件 (PRD 要求: 为 Agent 额外配置 env 环境, API Key 放入其中)
    if os.path.exists(env_path):
        load_dotenv(env_path)

    # 重新读取 DEFAULT_CONFIG (其内部已用 os.getenv 读取最新环境变量)
    # 为保证 .env 生效, 重新构建配置
    config = dict(DEFAULT_CONFIG)
    # 环境变量优先级最高, 重新覆盖一次
    config["llm_provider"] = os.getenv("LLM_PROVIDER", config["llm_provider"])
    config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", config["deep_think_llm"])
    config["quick_think_llm"] = os.getenv("QUICK_THINK_LLM", config["quick_think_llm"])
    config["backend_url"] = os.getenv("DEEPSEEK_BASE_URL", config["backend_url"])

    # 确保必要目录存在
    os.makedirs(config["results_dir"], exist_ok=True)
    os.makedirs(config["log_dir"], exist_ok=True)

    return config


def get_api_key(provider: str) -> str:
    """根据 provider 获取对应的 API Key (PRD: API Key 存放于 .env)

    Args:
        provider: 供应商名称 (deepseek/zhipu/tushare 等)

    Returns:
        API Key 字符串
    """
    key_map = {
        "deepseek": "DEEPSEEK_API_KEY",
        "glm": "ZHIPU_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
        "openai": "OPENAI_API_KEY",
        "tushare": "TUSHARE_TOKEN",
    }
    env_var = key_map.get(provider.lower(), f"{provider.upper()}_API_KEY")
    return os.getenv(env_var, "")
