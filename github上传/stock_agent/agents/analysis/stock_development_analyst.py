"""
个股发展信息分析师 (对应 PRD Phase 4 / M4b / 7.5, V2 新增角色)

采用 ReAct 模式分析个股发展性事件:
  - 重大公告: 重大合同(合同金额/年营收比)、并购重组、业绩预告
  - 研发进展: 新产品发布、技术突破、专利获批、临床试验进展(医药股)
  - 管理层变动: 核心高管变更、股权激励计划
  - 股东行为: 大股东增减持、高管减持、回购计划
  - 产业链事件: 上游涨价/降价、下游需求变化、竞争对手动态

可用工具 (PRD 8.7): announcement_search / news_search / research_report_search
催化作用评级: 强催化/中等催化/弱催化/无催化/负催化
"""
from loguru import logger

from stock_agent.agents.utils.prompts import STOCK_DEVELOPMENT_SYSTEM
from stock_agent.agents.utils.react_loop import run_react_loop
from stock_agent.agents.utils.json_helper import safe_json_parse_dict


def analyze_stock_development(llm, toolkit, ticker: str, name: str, trade_date: str, config: dict) -> dict:
    """对单只股票执行个股发展信息 ReAct 分析

    Returns:
        个股发展分析结果 dict: {rating, confidence, key_events, catalyst_assessment, risk_factors, report, react_iterations, tool_calls}
    """
    logger.info(f"[个股发展分析] 开始分析 {ticker} {name}")

    # === 获取可用工具 ===
    tools = toolkit.get_tools_for_analyst("stock_development")

    # === 构建 ReAct 用户查询 ===
    user_query = f"""请对 A 股股票 {name}（代码: {ticker}）进行个股发展信息分析。

分析日期: {trade_date}

请使用 ReAct 模式:
1. 思考: 识别需要检索哪些发展性事件
2. 行动: 调用工具获取数据:
   - announcement_search 检索公告 (⚠️ 仅调用 1 次, keyword 留空检索全部近 30 日公告, 一次获取所有类型)
   - news_search 搜索相关新闻 (仅 1 次)
   - research_report_search 检索研报 (可选, 仅在前两者无明确催化时调用)
   🔧 严格约束: 每个工具最多调用 1 次, 不要重复调用同一工具!
3. 观察: 分析工具返回的数据
4. 收到数据后立即生成分析报告, 不要再调用工具

最终输出严格 JSON:
{{
  "rating": "强催化/中等催化/弱催化/无催化/负催化",
  "confidence": 0.0到1.0,
  "key_events": [
    {{"type": "重大合同", "description": "...", "date": "...", "impact": "positive", "url": "..."}}
  ],
  "catalyst_assessment": "催化作用评估",
  "risk_factors": ["风险1"],
  "report": "详细发展信息分析报告"
}}

要求: 评级为"强催化"的发展事件须附原文链接。个股近30日无重大公告时输出"无催化"。"""

    # === 执行 ReAct 循环 ===
    analysis_text, tool_log = run_react_loop(
        llm=llm, tools=tools, system_prompt=STOCK_DEVELOPMENT_SYSTEM,
        user_query=user_query, config=config,
        analyst_name="stock_development",  # V2 优化: 读取 per-analyst ReAct 覆盖配置 (iter=3/total=5)
    )

    result = safe_json_parse_dict(analysis_text, default={
        "rating": "无催化",
        "confidence": 0.3,
        "catalyst_assessment": "数据不足, 近期无重大发展事件",
        "report": analysis_text,
        "risk_factors": ["数据不足"],
    })

    result["react_iterations"] = len(tool_log)
    result["tool_calls"] = tool_log

    logger.info(f"[个股发展分析] {ticker} 完成, 评级={result.get('rating','?')}, 工具调用={len(tool_log)}次")
    return result
