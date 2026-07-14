"""
基本面分析师 (对应 PRD Phase 4 / M4 / 8.5)

采用 ReAct 模式 (思考-行动-观察) 迭代分析股票基本面:
  - 财务状况: 营收/净利润/ROE/负债率/现金流
  - 盈利能力: 毛利率/净利率/ROE/ROA
  - 估值分析: PE/PB/PEG, 与行业均值对比
  - 投资建议: 买入/持有/卖出 + 目标价位

可用工具 (PRD 8.7): financial_data_query / industry_comparison / research_report_search

注意: 此模块不是图节点, 而是被 analysis_layer 调用的子函数。
"""
from loguru import logger

from stock_agent.agents.utils.prompts import FUNDAMENTALS_SYSTEM
from stock_agent.agents.utils.react_loop import run_react_loop
from stock_agent.agents.utils.json_helper import safe_json_parse_dict


def analyze_fundamentals(llm, toolkit, ticker: str, name: str, trade_date: str, config: dict) -> dict:
    """对单只股票执行基本面 ReAct 分析

    Args:
        llm: LLM 实例
        toolkit: Toolkit 工具包
        ticker: 股票代码
        name: 股票名称
        trade_date: 交易日期
        config: 系统配置 (含 ReAct 约束)

    Returns:
        基本面分析结果 dict: {rating, confidence, key_metrics, target_price, risks, report, react_iterations, tool_calls}
    """
    logger.info(f"[基本面分析] 开始分析 {ticker} {name}")

    # === 获取可用工具 (PRD 8.7: fundamentals 可用工具) ===
    tools = toolkit.get_tools_for_analyst("fundamentals")

    # === 构建 ReAct 用户查询 ===
    user_query = f"""请对 A 股股票 {name}（代码: {ticker}）进行基本面分析。

分析日期: {trade_date}

请使用 ReAct 模式:
1. 思考: 识别需要哪些财务数据
2. 行动: 调用工具获取数据 (financial_data_query 查财务指标, industry_comparison 行业对比, research_report_search 研报)
3. 观察: 分析工具返回的数据
4. 循环直至信息充分

最终输出严格 JSON:
{{
  "rating": "看多/中性/看空",
  "confidence": 0.0到1.0,
  "key_metrics": {{"roe": "...", "pe": "...", "pb": "..."}},
  "target_price": "目标价数值",
  "risks": ["风险1", "风险2"],
  "report": "详细分析报告"
}}

要求: 使用中文, 货币单位人民币(¥), 投资建议用中文(买入/持有/卖出)。"""

    # === 执行 ReAct 循环 (PRD 6.7) ===
    analysis_text, tool_log = run_react_loop(
        llm=llm,
        tools=tools,
        system_prompt=FUNDAMENTALS_SYSTEM,
        user_query=user_query,
        config=config,
        analyst_name="fundamentals",  # V2 优化: 读取 per-analyst ReAct 覆盖配置
    )

    # === 解析结果 (确保返回 dict, 修复 LLM 输出 JSON 数组导致的 'list indices' 错误) ===
    result = safe_json_parse_dict(analysis_text, default={
        "rating": "中性",
        "confidence": 0.3,
        "report": analysis_text,
        "risks": ["数据不足, 置信度低"],
    })

    # 补充 ReAct 元数据 (PRD 8.5: 含工具调用日志)
    result["react_iterations"] = len(tool_log)
    result["tool_calls"] = tool_log

    logger.info(f"[基本面分析] {ticker} 完成, 评级={result.get('rating','?')}, 工具调用={len(tool_log)}次")
    return result
