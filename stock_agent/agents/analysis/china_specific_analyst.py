"""
A 股专属分析师 (对应 PRD Phase 4 / M4 / 8.5)

采用 ReAct 模式分析 A 股特有维度:
  - 龙虎榜: 机构席位买卖动向, 游资接力情况
  - 北向资金: 沪股通/深股通资金流入流出
  - 融资融券: 融资余额变化, 做空压力
  - 涨跌停: 涨停封板强度, 连板情况

可用工具 (PRD 8.7): dragon_tiger_query / fund_flow_query
"""
from loguru import logger

from stock_agent.agents.utils.prompts import CHINA_SPECIFIC_SYSTEM
from stock_agent.agents.utils.react_loop import run_react_loop
from stock_agent.agents.utils.json_helper import safe_json_parse_dict


def analyze_china_specific(llm, toolkit, ticker: str, name: str, trade_date: str, config: dict) -> dict:
    """对单只股票执行 A 股专属维度 ReAct 分析

    Returns:
        A 股专属分析结果 dict: {rating, confidence, key_metrics, risks, report, react_iterations, tool_calls}
    """
    logger.info(f"[A股专属分析] 开始分析 {ticker} {name}")

    # === 获取可用工具 ===
    tools = toolkit.get_tools_for_analyst("china_specific")

    # === 构建 ReAct 用户查询 ===
    user_query = f"""请对 A 股股票 {name}（代码: {ticker}）进行 A 股专属维度分析。

分析日期: {trade_date}

请使用 ReAct 模式:
1. 思考: 识别需要哪些 A 股特有数据
2. 行动: 调用工具获取数据 (dragon_tiger_query 查龙虎榜, fund_flow_query 查资金流向)
3. 观察: 分析工具返回的数据
4. 循环直至信息充分

最终输出严格 JSON:
{{
  "rating": "看多/中性/看空",
  "confidence": 0.0到1.0,
  "key_metrics": {{"dragon_tiger": "...", "fund_flow": "..."}},
  "risks": ["风险1"],
  "report": "详细 A 股专属分析报告"
}}"""

    # === 执行 ReAct 循环 ===
    analysis_text, tool_log = run_react_loop(
        llm=llm, tools=tools, system_prompt=CHINA_SPECIFIC_SYSTEM,
        user_query=user_query, config=config,
        analyst_name="china_specific",  # V2 优化: 读取 per-analyst ReAct 覆盖配置
    )

    result = safe_json_parse_dict(analysis_text, default={
        "rating": "中性",
        "confidence": 0.3,
        "report": analysis_text,
        "risks": ["数据不足"],
    })

    result["react_iterations"] = len(tool_log)
    result["tool_calls"] = tool_log

    logger.info(f"[A股专属分析] {ticker} 完成, 评级={result.get('rating','?')}, 工具调用={len(tool_log)}次")
    return result
