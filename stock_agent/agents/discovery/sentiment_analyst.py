"""
情绪分析师 (对应 PRD Phase 1 / M1 热点发现引擎 / 8.1)

从股吧热帖和社交媒体讨论中分析市场情绪, 提取情绪极端的话题。
输出 hot_topics 列表 (source_type=social)。

图节点接口: create_sentiment_analyst(llm, toolkit) → sentiment_analyst_node(state) -> dict
"""
import json
import time
from loguru import logger

from stock_agent.agents.utils.prompts import SENTIMENT_ANALYST_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.log_utils import log_stage
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.dataflows import interface as data_interface


def create_sentiment_analyst(llm, toolkit):
    """创建情绪分析师图节点"""

    def sentiment_analyst_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[情绪分析师] 开始分析市场情绪")

        trade_date = state.get("trade_date", "")

        # === 获取社交媒体/股吧数据 (PRD M1) ===
        with log_stage("获取情绪数据", "情绪分析师") as stage:
            sentiment_data = _fetch_sentiment_data(trade_date)
            stage.result = f"{len(sentiment_data)} 字符"

        # === 构建 prompt ===
        prompt = f"""{SENTIMENT_ANALYST_SYSTEM}

分析日期: {trade_date}

股吧/社交媒体讨论数据:
{truncate_text(sentiment_data, 3000)}

请基于上述数据分析市场情绪, 输出严格 JSON:
{{
  "hot_topics": [
    {{
      "topic": "话题标题",
      "source_type": "social",
      "sector": "相关板块",
      "sentiment_score": -1.0到1.0,
      "momentum": 0到100,
      "mentioned_stocks": ["600584"],
      "reason": "判断依据"
    }}
  ]
}}

要求: 重点关注情绪极端(极度乐观或悲观)的话题, 这类往往预示反转。"""

        # === 调用 LLM ===
        with log_stage("LLM 情绪分析", "情绪分析师"):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"hot_topics": []}',
                node_name="情绪分析师",
            )
        result = safe_json_parse(response.content, default={"hot_topics": []})
        hot_topics = result.get("hot_topics", []) if isinstance(result, dict) else []

        for topic in hot_topics:
            if isinstance(topic, dict):
                topic.setdefault("source_type", "social")

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[情绪分析师] 完成, 耗时 {node_elapsed:.2f}s, 提取 {len(hot_topics)} 条情绪热点")

        # 仅返回本节点新增的热点 (operator.add reducer 自动拼接三路发现的结果)
        return {"hot_topics": hot_topics}

    return sentiment_analyst_node


def _fetch_sentiment_data(trade_date: str) -> str:
    """获取股吧/社交媒体数据 (通过新闻接口降级)"""
    try:
        # 尝试获取个股新闻作为情绪数据源 (股吧数据 AkShare 可能不直接提供)
        result = data_interface.get_news(keyword="股吧", count=20)
        if result and result.get("data"):
            return json.dumps(result["data"], ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[情绪分析师] 获取情绪数据失败: {e}")
    return "(暂无可用股吧/社媒数据, 请基于常识推理当前市场情绪)"
