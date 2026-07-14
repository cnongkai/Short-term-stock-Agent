"""
政策分析师 (对应 PRD Phase 1 / 6.3 / M1, V2 新增角色)

从证监会、央行、交易所等权威信源提取政策事件, 评估影响方向与力度。
输出 policy_events 列表 + 政策类 hot_topics (source_type=policy, 享 1.2 倍权重)。

6 类权威信源 (PRD 6.3):
  1. 证监会 (csrc.gov.cn)
  2. 央行 (pbc.gov.cn)
  3. 上交所/深交所
  4. 巨潮资讯 (cninfo)
  5. 四大证券报 (中证报/上证报/证券时报/证券日报)
  6. 新华社财经

合规: robots.txt 遵守 + 限速(≤1次/5分钟) + 摘要提取 + URL 标注

图节点接口: create_policy_analyst(llm, toolkit) → policy_analyst_node(state) -> dict
"""
import json
import time
from loguru import logger

from stock_agent.agents.utils.prompts import POLICY_ANALYST_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.log_utils import log_stage
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.dataflows import interface as data_interface


def create_policy_analyst(llm, toolkit):
    """创建政策分析师图节点 (V2 新增)"""

    def policy_analyst_node(state) -> dict:
        node_start = time.perf_counter()
        logger.info("[政策分析师] 开始提取政策事件 (V2 新增)")

        trade_date = state.get("trade_date", "")

        # === 获取政策事件数据 (PRD 6.3, 6 类权威信源) ===
        with log_stage("获取政策数据", "政策分析师") as stage:
            policy_data = _fetch_policy_data()
            stage.result = f"{len(policy_data)} 字符"

        # === 构建 prompt (PRD 8.2) ===
        prompt = f"""{POLICY_ANALYST_SYSTEM}

分析日期: {trade_date}

最新政策事件数据 (6 类权威信源):
{truncate_text(policy_data, 3000)}

请基于上述数据提取政策事件, 输出严格 JSON:
{{
  "policy_events": [
    {{
      "title": "政策标题",
      "source": "pbc.gov.cn",
      "authority_level": "央行",
      "publish_time": "2026-07-08T07:30:00",
      "affected_sectors": ["银行"],
      "impact_direction": "利好",
      "impact_strength": "强",
      "rationale": "影响推理依据",
      "affected_stocks": ["600036"],
      "url": "https://..."
    }}
  ],
  "hot_topics": [
    {{
      "topic": "政策话题标题",
      "source_type": "policy",
      "authority_level": "央行",
      "sector": "银行",
      "sentiment_score": 0.85,
      "momentum": 92,
      "impact_direction": "利好",
      "impact_strength": "强",
      "mentioned_stocks": ["600036"],
      "reason": "判断依据"
    }}
  ]
}}

要求: 央行或国务院级别的利好政策对关联板块赋予最高政策影响分。
政策热点在聚合时享有 1.2 倍权重加成 (PRD 7.1)。"""

        # === 调用 LLM ===
        with log_stage("LLM 政策分析", "政策分析师"):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"policy_events": [], "hot_topics": []}',
                node_name="政策分析师",
            )
        result = safe_json_parse(response.content, default={"policy_events": [], "hot_topics": []})

        policy_events = result.get("policy_events", []) if isinstance(result, dict) else []
        hot_topics = result.get("hot_topics", []) if isinstance(result, dict) else []

        # 标记来源
        for topic in hot_topics:
            if isinstance(topic, dict):
                topic.setdefault("source_type", "policy")

        node_elapsed = time.perf_counter() - node_start
        logger.info(f"[政策分析师] 完成, 耗时 {node_elapsed:.2f}s, "
                    f"提取 {len(policy_events)} 条政策事件, {len(hot_topics)} 条政策热点")

        # 仅返回本节点新增的数据 (operator.add reducer 自动拼接三路发现的结果)
        return {
            "policy_events": policy_events,
            "hot_topics": hot_topics,
        }

    return policy_analyst_node


def _fetch_policy_data() -> str:
    """获取政策事件数据 (通过政策信源管理器)"""
    try:
        result = data_interface.fetch_policy_events(max_items=20)
        if result and result.get("data"):
            return json.dumps(result["data"], ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[政策分析师] 获取政策数据失败: {e}")
    return "(暂无可用政策数据, 政策信源可能未启用或全部故障。请基于常识推理近期可能的政策热点)"
