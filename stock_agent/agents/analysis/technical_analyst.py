"""
技术分析师 (对应 PRD Phase 4 / M4 / 8.5)

采用 ReAct 模式迭代分析股票技术面:
  - 趋势分析: 均线系统(5/10/20/60日)多空排列
  - 指标分析: MACD/RSI/KDJ/BOLL 信号判断
  - 量价关系: 量价配合度, 异常成交
  - 关键位: 支撑位/压力位/突破信号

可用工具 (PRD 8.7): technical_indicator_calc / news_search
"""
from loguru import logger

from stock_agent.agents.utils.prompts import TECHNICAL_SYSTEM
from stock_agent.agents.utils.react_loop import run_react_loop
from stock_agent.agents.utils.json_helper import safe_json_parse_dict


def analyze_technical(llm, toolkit, ticker: str, name: str, trade_date: str, config: dict) -> dict:
    """对单只股票执行技术面 ReAct 分析

    Args:
        llm, toolkit, ticker, name, trade_date, config: 同 analyze_fundamentals

    Returns:
        技术分析结果 dict: {rating, confidence, key_metrics, target_price, support_price, resistance_price, risks, report, react_iterations, tool_calls}
    """
    logger.info(f"[技术分析] 开始分析 {ticker} {name}")

    # === 获取可用工具 ===
    tools = toolkit.get_tools_for_analyst("technical")

    # === 构建 ReAct 用户查询 ===
    user_query = f"""请对 A 股股票 {name}（代码: {ticker}）进行技术面分析。

分析日期: {trade_date}

请使用 ReAct 模式:
1. 思考: 识别需要哪些行情和指标数据
2. 行动: 调用工具获取数据 (technical_indicator_calc 计算技术指标, news_search 搜索相关新闻)
3. 观察: 分析工具返回的数据
4. 循环直至信息充分

最终输出严格 JSON:
{{
  "rating": "看多/中性/看空",
  "confidence": 0.0到1.0,
  "key_metrics": {{"macd": "...", "rsi": "...", "trend": "..."}},
  "target_price": "目标价数值",
  "support_price": "支撑价位数值",
  "resistance_price": "压力价位数值",
  "risks": ["风险1"],
  "report": "详细技术分析报告"
}}"""

    # === 执行 ReAct 循环 ===
    analysis_text, tool_log = run_react_loop(
        llm=llm, tools=tools, system_prompt=TECHNICAL_SYSTEM,
        user_query=user_query, config=config,
        analyst_name="technical",  # V2 优化: 读取 per-analyst ReAct 覆盖配置
    )

    result = safe_json_parse_dict(analysis_text, default={
        "rating": "中性",
        "confidence": 0.3,
        "report": analysis_text,
        "risks": ["数据不足"],
    })

    result["react_iterations"] = len(tool_log)
    result["tool_calls"] = tool_log

    logger.info(f"[技术分析] {ticker} 完成, 评级={result.get('rating','?')}, 工具调用={len(tool_log)}次")
    return result
