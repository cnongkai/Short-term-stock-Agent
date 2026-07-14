"""
交易代理 (对应 PRD Phase 6 / M6)

综合投资计划与四维分析报告, 对候选池逐只产出交易决策。
输出 trader_investment_plan (JSON 字符串, 含逐只决策数组:
  action/target_price/take_profit/stop_loss/confidence/risk_level)。

PRD 约束:
  - action 必须明确 (买入/持有/卖出), 禁止 null
  - 必须提供具体数值的目标价/止盈/止损

参考架构: TradingAgents-CN agents/trader/trader.py
"""
import json
from loguru import logger

from stock_agent.agents.utils.prompts import TRADER_SYSTEM
from stock_agent.agents.utils.json_helper import safe_json_parse, truncate_text
from stock_agent.agents.utils.llm_utils import safe_llm_invoke
from stock_agent.graph.run_metrics import get_metrics


def create_trader(llm):
    """创建交易代理图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)

    Returns:
        trader_node(state) -> dict: 更新 trader_investment_plan
    """

    def trader_node(state) -> dict:
        logger.info("[交易代理] 开始生成逐只交易决策")

        metrics = get_metrics()

        # === 读取状态 ===
        investment_plan = state.get("investment_plan", "{}")
        analysis_reports = state.get("analysis_reports", {})
        candidate_pool = state.get("candidate_pool", [])

        # === 构建候选池分析摘要 ===
        reports_summary = _build_reports_summary(analysis_reports, candidate_pool)

        # === 构建 prompt (单次调用, 产出全部标的决策) ===
        prompt = f"""{TRADER_SYSTEM}

研究经理投资计划:
{truncate_text(investment_plan, 2000)}

候选池四维分析报告:
{reports_summary}

请对上述 {len(candidate_pool)} 只标的逐只给出交易决策。
输出严格 JSON, 格式:
{{
  "decisions": [
    {{
      "ticker": "代码",
      "name": "名称",
      "source": "hot_sector 或 dark_horse",
      "action": "买入/持有/卖出",
      "target_price": "目标价(数值)",
      "take_profit": "止盈价(数值)",
      "stop_loss": "止损价(数值)",
      "confidence": 0.0到1.0,
      "risk_level": 0.0到1.0,
      "reasoning": "决策推理"
    }}
  ]
}}

要求:
- action 禁止 null, 必须明确买入/持有/卖出
- 目标价/止盈/止损必须为具体数值
- 使用中文, 货币单位人民币(¥)"""

        # === 调用 LLM + 节点计时 + Token 估算 ===
        with metrics.node_timer("交易代理"), metrics.llm_timer("交易代理", prompt):
            response = safe_llm_invoke(
                llm, prompt,
                timeout=90, max_retries=2,
                fallback_content='{"decisions": []}',
                node_name="交易代理",
                on_fallback=metrics.record_timeout,
            )
        content = response.content
        metrics.record_llm_result("交易代理", content)

        # === 解析 JSON ===
        result = safe_json_parse(content, default={"decisions": []})
        decisions = result.get("decisions", []) if isinstance(result, dict) else []

        # === 后处理: 校验并补全缺失字段 ===
        decisions = _validate_decisions(decisions, candidate_pool)

        trader_plan = json.dumps({"decisions": decisions}, ensure_ascii=False)
        logger.info(f"[交易代理] 生成 {len(decisions)} 只标的的交易决策")

        return {"trader_investment_plan": trader_plan}

    return trader_node


def _build_reports_summary(analysis_reports: dict, candidate_pool: list) -> str:
    """构建候选池四维分析摘要 (供交易决策引用)"""
    if not candidate_pool:
        return "(候选池为空)"

    lines = []
    for stock in candidate_pool:
        ticker = stock.get("ticker", "?")
        name = stock.get("name", "?")
        source = stock.get("source", "?")
        report = analysis_reports.get(ticker, {})

        lines.append(f"\n--- {ticker} {name} [{source}] ---")
        for dim in ["fundamentals", "technical", "china_specific", "stock_development"]:
            dim_report = report.get(dim, {})
            if isinstance(dim_report, dict):
                rating = dim_report.get("rating", "?")
                target = dim_report.get("target_price", "")
                lines.append(f"  {dim}: 评级={rating}" + (f" 目标价={target}" if target else ""))

    return "\n".join(lines) if lines else "(无分析数据)"


def _validate_decisions(decisions: list, candidate_pool: list) -> list:
    """校验交易决策, 补全缺失字段 (PRD: action 禁止 null)"""
    validated = []
    for dec in decisions:
        if not isinstance(dec, dict):
            continue
        # 确保 action 不为空
        if not dec.get("action"):
            dec["action"] = "持有"
        # 确保数值字段存在
        for field in ["target_price", "take_profit", "stop_loss"]:
            if not dec.get(field):
                dec[field] = "0"
        validated.append(dec)

    # 确保候选池中的每只标的都有决策
    existing_tickers = {d.get("ticker") for d in validated}
    for stock in candidate_pool:
        ticker = stock.get("ticker", "")
        if ticker and ticker not in existing_tickers:
            validated.append({
                "ticker": ticker,
                "name": stock.get("name", ""),
                "source": stock.get("source", ""),
                "action": "持有",
                "target_price": "0",
                "take_profit": "0",
                "stop_loss": "0",
                "confidence": 0.0,
                "risk_level": 1.0,
                "reasoning": "数据不足, 默认持有",
            })

    return validated
