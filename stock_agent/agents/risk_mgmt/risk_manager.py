"""
风险经理 / 风险裁决 (对应 PRD Phase 7 / M7 / 10.2)

综合三风格风险辩论, 对逐只标的做出最终审批决定 (批准/拒绝/调整)。
输出 final_trade_decision (JSON 字符串, 含逐只审批结果)。

PRD 约束:
  - 辩论不收敛时默认保守"拒绝" (PRD 10.2)
  - 黑马推荐执行更严格风控标准 (PRD 11.2)

参考架构: TradingAgents-CN agents/risk_mgmt/risk_manager.py
"""
import json
from loguru import logger

from stock_agent.agents.utils.prompts import RISK_MANAGER_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.graph.run_metrics import get_metrics


def create_risk_manager(llm):
    """创建风险经理图节点 (图中节点名 "Risk Judge")

    Args:
        llm: LLM 实例 (deep_thinking_llm, 深度思考)

    Returns:
        risk_manager_node(state) -> dict: 更新 final_trade_decision + risk_debate_state.judge_decision
    """

    def risk_manager_node(state) -> dict:
        logger.info("[风险经理] 开始逐只审批交易决策")

        metrics = get_metrics()

        # === 读取状态 ===
        risk_state = state.get("risk_debate_state", {})
        history = risk_state.get("history", "")

        trader_plan = state.get("trader_investment_plan", "{}")
        candidate_pool = state.get("candidate_pool", [])

        # === 构建 prompt ===
        prompt = f"""{RISK_MANAGER_SYSTEM}

风险辩论完整历史 (激进/保守/中立三方):
{truncate_text(history, 4000)}

交易代理的逐只交易决策:
{truncate_text(trader_plan, 3000)}

请综合三方辩论, 对每只标的做出最终审批决定。
输出严格 JSON:
{{
  "decisions": [
    {{
      "ticker": "代码",
      "name": "名称",
      "decision": "批准/拒绝/调整",
      "risk_assessment": "风险评估",
      "conditions": ["附加条件1", "条件2"],
      "reasoning": "决策依据"
    }}
  ],
  "overall_risk_level": 0.0到1.0
}}

要求:
- 辩论不收敛时默认保守"拒绝" (PRD 10.2)
- 黑马(source=dark_horse)执行更严格风控标准 (PRD 11.2)
- decision 禁止 null, 必须明确批准/拒绝/调整"""

        # === 调用 LLM (深度思考) + 节点计时 + Token 估算 ===
        with metrics.node_timer("风险经理"), metrics.llm_timer("风险经理", prompt):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"decisions": [], "overall_risk_level": 0.8}',
                node_name="风险经理",
                on_fallback=metrics.record_timeout,
            )
        content = response.content
        metrics.record_llm_result("风险经理", content)

        # === 解析 JSON ===
        result = safe_json_parse(content, default={"decisions": [], "overall_risk_level": 0.8})
        decisions = result.get("decisions", []) if isinstance(result, dict) else []

        # === 后处理: 确保候选池中每只标的都有审批结果 ===
        decisions = _validate_risk_decisions(decisions, candidate_pool)

        final_decision = json.dumps({
            "decisions": decisions,
            "overall_risk_level": result.get("overall_risk_level", 0.8) if isinstance(result, dict) else 0.8,
        }, ensure_ascii=False)

        approved = sum(1 for d in decisions if d.get("decision") == "批准")
        logger.info(f"[风险经理] 审批完成: 批准 {approved}/{len(decisions)} 只")

        # === 更新状态 ===
        new_risk_state = dict(risk_state)
        new_risk_state["judge_decision"] = final_decision

        return {
            "final_trade_decision": final_decision,
            "risk_debate_state": new_risk_state,
        }

    return risk_manager_node


def _validate_risk_decisions(decisions: list, candidate_pool: list) -> list:
    """校验风险审批结果, 补全缺失标的 (PRD: 默认保守拒绝)"""
    validated = []
    for dec in decisions:
        if not isinstance(dec, dict):
            continue
        if not dec.get("decision"):
            dec["decision"] = "拒绝"
        validated.append(dec)

    # 确保候选池中每只标的都有审批结果 (缺失默认拒绝, PRD 10.2)
    existing_tickers = {d.get("ticker") for d in validated}
    for stock in candidate_pool:
        ticker = stock.get("ticker", "")
        if ticker and ticker not in existing_tickers:
            validated.append({
                "ticker": ticker,
                "name": stock.get("name", ""),
                "decision": "拒绝",
                "risk_assessment": "数据不足, 默认保守拒绝",
                "conditions": [],
                "reasoning": "缺失审批结果, 按 PRD 10.2 默认拒绝",
            })

    return validated
