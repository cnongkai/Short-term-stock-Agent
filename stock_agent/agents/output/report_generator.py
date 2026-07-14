"""
报告生成器 (对应 PRD Phase 8 / 全链路留痕)

读取全部状态字段, 生成 Markdown 推荐报告 + JSON 报告, 写入 results/ 目录。
无需 LLM 调用, 纯数据聚合与格式化。

输出文件:
  - results/recommendation_{date}.md  : 人类可读的 Markdown 报告
  - results/recommendation_{date}.json: 机器可读的 JSON 报告 (含全链路状态)
"""
import os
import json
from datetime import datetime
from loguru import logger


def create_report_generator():
    """创建报告生成器图节点 (无需 LLM)

    Returns:
        report_generator_node(state) -> dict: 无状态更新 (仅写文件)
    """

    def report_generator_node(state) -> dict:
        logger.info("[报告生成] 开始生成推荐报告")

        trade_date = state.get("trade_date", "unknown")
        portfolio = state.get("final_portfolio", [])
        candidate_pool = state.get("candidate_pool", [])
        ranked_sectors = state.get("ranked_sectors", [])
        circuit_breaker = state.get("circuit_breaker", False)

        # === 确定输出目录 ===
        results_dir = os.getenv("RESULTS_DIR", "./results")
        os.makedirs(results_dir, exist_ok=True)

        # === 生成 Markdown 报告 ===
        md_report = _build_markdown_report(state)
        md_path = os.path.join(results_dir, f"recommendation_{trade_date}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_report)
        logger.info(f"[报告生成] Markdown 报告: {md_path}")

        # === 生成 JSON 报告 (含全链路状态摘要) ===
        json_report = _build_json_report(state)
        json_path = os.path.join(results_dir, f"recommendation_{trade_date}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_report, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[报告生成] JSON 报告: {json_path}")

        return {}  # 报告生成不更新状态

    return report_generator_node


def _build_markdown_report(state) -> str:
    """构建 Markdown 格式推荐报告"""
    trade_date = state.get("trade_date", "unknown")
    portfolio = state.get("final_portfolio", [])
    candidate_pool = state.get("candidate_pool", [])
    ranked_sectors = state.get("ranked_sectors", [])
    hot_topics = state.get("hot_topics", [])
    policy_events = state.get("policy_events", [])
    circuit_breaker = state.get("circuit_breaker", False)
    investment_plan = state.get("investment_plan", "")
    final_decision = state.get("final_trade_decision", "")

    lines = [
        f"# 短线股票推荐报告 — {trade_date}",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 候选池: {len(candidate_pool)} 只 | 推荐标的: {len(portfolio)} 只",
        "",
    ]

    # === 熔断提示 ===
    if circuit_breaker:
        lines += [
            "## ⚠️ 熔断提示",
        "> 大盘单日跌幅超过阈值, 系统已暂停推荐 (PRD 10.2)",
            "",
        ]

    # === 推荐清单 ===
    lines += ["## 📊 最终推荐清单", ""]
    if portfolio:
        lines += [
            "| # | 代码 | 名称 | 来源 | 评级 | 目标价 | 止盈 | 止损 | 置信度 |",
            "|---|------|------|------|------|--------|------|------|--------|",
        ]
        for i, stock in enumerate(portfolio, 1):
            lines.append(
                f"| {i} | {stock.get('ticker','')} | {stock.get('name','')} | "
                f"{stock.get('source','')} | {stock.get('rating','')} | "
                f"¥{stock.get('target_price','')} | ¥{stock.get('take_profit','')} | "
                f"¥{stock.get('stop_loss','')} | {stock.get('confidence','')} |"
            )
    else:
        lines.append("本周期无推荐标的。")
    lines.append("")

    # === 热门板块 ===
    if ranked_sectors:
        lines += ["## 🔥 热门板块排名", ""]
        for i, sector in enumerate(ranked_sectors[:5], 1):
            lines.append(
                f"{i}. **{sector.get('sector','')}** — "
                f"热度: {sector.get('heat_score','')} | "
                f"趋势: {sector.get('trend','')} | "
                f"政策影响: {sector.get('policy_impact','')}"
            )
        lines.append("")

    # === 热点话题 ===
    if hot_topics:
        lines += ["## 📰 热点话题", ""]
        for topic in hot_topics[:10]:
            lines.append(
                f"- [{topic.get('source_type','')}] {topic.get('topic','')} "
                f"(情绪: {topic.get('sentiment_score','')}, 动量: {topic.get('momentum','')})"
            )
        lines.append("")

    # === 政策事件 ===
    if policy_events:
        lines += ["## 🏛️ 政策事件", ""]
        for event in policy_events[:5]:
            lines.append(
                f"- **{event.get('title','')}** — {event.get('authority_level','')} | "
                f"影响: {event.get('impact_direction','')}({event.get('impact_strength','')}) | "
                f"板块: {', '.join(event.get('affected_sectors',[]))}"
            )
        lines.append("")

    # === 候选池详情 ===
    if candidate_pool:
        lines += ["## 📋 候选池详情", ""]
        for stock in candidate_pool:
            lines.append(
                f"- {stock.get('ticker','')} {stock.get('name','')} "
                f"[{stock.get('source','')}] — {stock.get('reason','')}"
            )
        lines.append("")

    # === 投资计划摘要 ===
    if investment_plan:
        lines += ["## 📝 投资计划摘要", "", "```json", investment_plan[:2000], "```", ""]

    # === 风控决策摘要 ===
    if final_decision:
        lines += ["## 🛡️ 风控决策摘要", "", "```json", final_decision[:2000], "```", ""]

    lines.append("---")
    lines.append("> 本报告由短线股票推荐 Agent (PRD V2) 自动生成, 仅供参考, 不构成投资建议。")

    return "\n".join(lines)


def _build_json_report(state) -> dict:
    """构建 JSON 格式报告 (含全链路状态摘要)"""
    # 过滤不可序列化的 messages 字段
    serializable = {}
    for k, v in state.items():
        if k == "messages":
            continue
        try:
            json.dumps(v, default=str)
            serializable[k] = v
        except (TypeError, ValueError):
            serializable[k] = str(v)

    return {
        "trade_date": state.get("trade_date", ""),
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "candidate_count": len(state.get("candidate_pool", [])),
            "recommended_count": len(state.get("final_portfolio", [])),
            "circuit_breaker": state.get("circuit_breaker", False),
            "hot_topics_count": len(state.get("hot_topics", [])),
            "policy_events_count": len(state.get("policy_events", [])),
            "ranked_sectors_count": len(state.get("ranked_sectors", [])),
        },
        "final_portfolio": state.get("final_portfolio", []),
        "full_state": serializable,
    }
