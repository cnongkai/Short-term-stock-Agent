"""
新闻分析师 (对应 PRD Phase 1 / M1 热点发现引擎 / 8.1)

从财经新闻中提取短线热点话题, 识别可能影响短线行情的话题。
输出 hot_topics 列表 (含 topic/source_type/sector/sentiment_score/momentum/mentioned_stocks)。

图节点接口: create_news_analyst(llm, toolkit) → news_analyst_node(state) -> dict
"""
import json
import time
from loguru import logger

from stock_agent.agents.utils.prompts import NEWS_ANALYST_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.log_utils import log_stage
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.dataflows import interface as data_interface


def create_news_analyst(llm, toolkit):
    """创建新闻分析师图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)
        toolkit: Toolkit 工具包 (含 news_search 工具)

    Returns:
        news_analyst_node(state) -> dict: 更新 hot_topics
    """

    def news_analyst_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[新闻分析师] 开始提取财经新闻热点")

        trade_date = state.get("trade_date", "")

        # === 获取财经新闻数据 (PRD M1) ===
        with log_stage("获取新闻数据", "新闻分析师") as stage:
            news_data = _fetch_news(trade_date)
            stage.result = f"{len(news_data)} 字符"

        # === 构建 prompt (PRD 8.1) ===
        prompt = f"""{NEWS_ANALYST_SYSTEM}

分析日期: {trade_date}

今日财经新闻数据:
{truncate_text(news_data, 3000)}

请基于上述新闻数据提取热点话题, 输出严格 JSON:
{{
  "hot_topics": [
    {{
      "topic": "话题标题",
      "source_type": "news",
      "sector": "相关板块",
      "sentiment_score": -1.0到1.0,
      "momentum": 0到100,
      "mentioned_stocks": ["600584"],
      "reason": "判断依据"
    }}
  ]
}}"""

        # === 调用 LLM 提取热点 ===
        with log_stage("LLM 提取热点", "新闻分析师"):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"hot_topics": []}',
                node_name="新闻分析师",
            )
        result = safe_json_parse(response.content, default={"hot_topics": []})
        hot_topics = result.get("hot_topics", []) if isinstance(result, dict) else []

        # 标记来源
        for topic in hot_topics:
            if isinstance(topic, dict):
                topic.setdefault("source_type", "news")

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[新闻分析师] 完成, 耗时 {node_elapsed:.2f}s, 提取 {len(hot_topics)} 条新闻热点")

        # 仅返回本节点新增的热点 (operator.add reducer 自动拼接三路发现的结果)
        return {"hot_topics": hot_topics}

    return news_analyst_node


def _fetch_news(trade_date: str) -> str:
    """获取今日财经新闻数据 (三级 fallback: AkShare → WebNews RSS → 政策事件)"""
    # 第一层: AkShare/WebNews provider 链
    try:
        result = data_interface.get_news(keyword="", count=30)
        if result and result.get("data"):
            logger.info(f"[新闻分析师] 从 provider 链获取 {len(result['data'])} 条新闻")
            return json.dumps(result["data"], ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[新闻分析师] provider 链获取新闻失败: {e}")

    # 第二层: 政策事件作为新闻 fallback (政策信源可能包含新闻类数据)
    try:
        policy_result = data_interface.fetch_policy_events(max_items=20)
        if policy_result and policy_result.get("data"):
            logger.info(f"[新闻分析师] 使用政策事件作为新闻 fallback, {len(policy_result['data'])} 条")
            policy_as_news = []
            for event in policy_result["data"]:
                policy_as_news.append({
                    "title": event.get("title", ""),
                    "summary": event.get("summary", ""),
                    "source": event.get("source", ""),
                    "published": event.get("publish_time", ""),
                    "category": "政策新闻",
                })
            return json.dumps(policy_as_news, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[新闻分析师] 政策事件 fallback 失败: {e}")

    # 第三层: 返回空提示文本 (让 LLM 基于常识推理)
    return "(暂无可用新闻数据, 请基于常识推理当日可能的热点)"
