"""
图构建 (对应 PRD 第6章核心流程设计 / 6.2 系统架构图)

GraphSetup 负责组装 LangGraph 状态图:
  START → 发现层(四路并行: 新闻/情绪/政策分析师 + 板块推理[V2 纯量化])
       → 双路选股(主线选股 + 黑马扫描, fan-in 同步等待四路完成)
       → 合并 → 分析层(四维 ReAct, 遍历候选池)
       → 决策层(多空辩论→交易→三风格风控→组合经理, 遍历推荐标的)
       → 报告生成 → END

V2 变更: 板块推理从"下游"改为"第四路并行", 不再依赖新闻/情绪/政策输出,
        采用纯量化五维赋分(热度/扩散力/动摇度/回补力/拥挤度)。
主图采用顺序编排, 分析/决策层内部遍历候选池 (功能等价于并行, 便于运行验证)。
参考架构: TradingAgents-CN graph/setup.py
"""
from typing import Dict, Any

from langgraph.graph import END, StateGraph, START

from stock_agent.agents.utils.agent_states import AgentState
from stock_agent.agents.utils.agent_utils import Toolkit
from stock_agent.graph.conditional_logic import ConditionalLogic

from loguru import logger


class GraphSetup:
    """组装短线推荐 Agent 工作流图"""

    def __init__(
        self,
        quick_thinking_llm,
        deep_thinking_llm,
        toolkit: Toolkit,
        conditional_logic: ConditionalLogic,
        config: Dict[str, Any] = None,
    ):
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.toolkit = toolkit
        self.conditional_logic = conditional_logic
        self.config = config or {}

    def setup_graph(self) -> Any:
        """构建并编译工作流图

        Returns:
            编译后的 LangGraph 可执行图
        """
        # === 创建各层节点 (懒导入避免循环依赖) ===
        # 发现层 (PRD Phase 1-3)
        from stock_agent.agents.discovery.news_analyst import create_news_analyst
        from stock_agent.agents.discovery.sentiment_analyst import create_sentiment_analyst
        from stock_agent.agents.discovery.policy_analyst import create_policy_analyst
        from stock_agent.agents.discovery.sector_inference import create_sector_inference
        from stock_agent.agents.discovery.stock_selector import create_stock_selector
        from stock_agent.agents.discovery.dark_horse_scanner import create_dark_horse_scanner
        from stock_agent.agents.discovery.pool_merger import create_pool_merger

        # 分析层 (PRD Phase 4, 四维 ReAct)
        from stock_agent.agents.analysis.analysis_layer import create_analysis_layer

        # 决策层 (PRD Phase 5-7)
        from stock_agent.agents.researchers.bull_researcher import create_bull_researcher
        from stock_agent.agents.researchers.bear_researcher import create_bear_researcher
        from stock_agent.agents.researchers.research_manager import create_research_manager
        from stock_agent.agents.trader.trader import create_trader
        from stock_agent.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
        from stock_agent.agents.risk_mgmt.conservative_debator import create_conservative_debator
        from stock_agent.agents.risk_mgmt.neutral_debator import create_neutral_debator
        from stock_agent.agents.risk_mgmt.risk_manager import create_risk_manager
        from stock_agent.agents.risk_mgmt.portfolio_manager import create_portfolio_manager

        # 输出层 (PRD Phase 8)
        from stock_agent.agents.output.report_generator import create_report_generator

        # === 创建工作流 ===
        workflow = StateGraph(AgentState)

        # --- 发现层节点 (PRD Phase 1-3) ---
        workflow.add_node("News Analyst", create_news_analyst(self.quick_thinking_llm, self.toolkit))
        workflow.add_node("Sentiment Analyst", create_sentiment_analyst(self.quick_thinking_llm, self.toolkit))
        workflow.add_node("Policy Analyst", create_policy_analyst(self.quick_thinking_llm, self.toolkit))
        workflow.add_node("Sector Inference", create_sector_inference(self.quick_thinking_llm, self.config))
        workflow.add_node("Stock Selector", create_stock_selector(self.quick_thinking_llm))
        workflow.add_node("Dark Horse Scanner", create_dark_horse_scanner(self.quick_thinking_llm))
        workflow.add_node("Pool Merger", create_pool_merger())

        # --- 分析层节点 (PRD Phase 4) ---
        # analysis_layer 内部遍历候选池, 对每只股票跑4个 ReAct 分析师
        workflow.add_node("Analysis Layer", create_analysis_layer(
            self.quick_thinking_llm, self.toolkit, self.config
        ))

        # --- 决策层节点 (PRD Phase 5-7) ---
        workflow.add_node("Bull Researcher", create_bull_researcher(self.quick_thinking_llm))
        workflow.add_node("Bear Researcher", create_bear_researcher(self.quick_thinking_llm))
        workflow.add_node("Research Manager", create_research_manager(self.deep_thinking_llm))
        workflow.add_node("Trader", create_trader(self.quick_thinking_llm))
        workflow.add_node("Aggressive Analyst", create_aggressive_debator(self.quick_thinking_llm))
        workflow.add_node("Conservative Analyst", create_conservative_debator(self.quick_thinking_llm))
        workflow.add_node("Neutral Analyst", create_neutral_debator(self.quick_thinking_llm))
        workflow.add_node("Risk Judge", create_risk_manager(self.deep_thinking_llm))
        workflow.add_node("Portfolio Manager", create_portfolio_manager(self.deep_thinking_llm))

        # --- 输出层节点 (PRD Phase 8) ---
        workflow.add_node("Report Generator", create_report_generator())
        # 熔断暂停节点 (PRD 10.2)
        workflow.add_node("Circuit Breaker Pause", _circuit_breaker_pause_node)

        # === 定义边 (PRD 6.4 端到端流程) ===
        # Phase 1-2: 四路并行发现 (V2: 板块推理改为与新闻/情绪/政策并行, 不再依赖其输出)
        #   - News/Sentiment/Policy 写 hot_topics/policy_events (operator.add 累积)
        #   - Sector Inference 写 ranked_sectors/sector_crowding_map (纯量化, 不依赖 LLM)
        # LangGraph 语义: 多个节点同时连向同一目标时, 目标会等待所有前驱完成 (fan-in 同步)
        workflow.add_edge(START, "News Analyst")
        workflow.add_edge(START, "Sentiment Analyst")
        workflow.add_edge(START, "Policy Analyst")
        workflow.add_edge(START, "Sector Inference")  # V2: 第四路并行

        # Phase 3: 四路汇合 → 双路选股 (fan-in 同步等待全部完成)
        # Stock Selector / Dark Horse Scanner 读取 ranked_sectors + hot_topics,
        # 需等待四路全部产出后才启动
        for _analyst in ["News Analyst", "Sentiment Analyst", "Policy Analyst", "Sector Inference"]:
            workflow.add_edge(_analyst, "Stock Selector")
            workflow.add_edge(_analyst, "Dark Horse Scanner")

        # 双路选股 → 合并
        workflow.add_edge("Stock Selector", "Pool Merger")
        workflow.add_edge("Dark Horse Scanner", "Pool Merger")

        # 合并 → 分析层 (条件: 候选池非空且未熔断)
        workflow.add_conditional_edges(
            "Pool Merger",
            self.conditional_logic.should_run_analysis,
            {
                "Analysis Layer": "Analysis Layer",
                "Report Generator": "Report Generator",
                "Circuit Breaker Pause": "Circuit Breaker Pause",
            },
        )

        # Phase 4 → Phase 5: 分析层 → 多空辩论
        workflow.add_edge("Analysis Layer", "Bull Researcher")

        # Phase 5: 多空辩论循环 (条件边)
        workflow.add_conditional_edges(
            "Bull Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bear Researcher": "Bear Researcher",
                "Research Manager": "Research Manager",
            },
        )
        workflow.add_conditional_edges(
            "Bear Researcher",
            self.conditional_logic.should_continue_debate,
            {
                "Bull Researcher": "Bull Researcher",
                "Research Manager": "Research Manager",
            },
        )

        # Phase 5 → Phase 6: 研究经理裁决 → 交易代理
        workflow.add_edge("Research Manager", "Trader")

        # Phase 6 → Phase 7: 交易代理 → 风险辩论
        workflow.add_edge("Trader", "Aggressive Analyst")
        workflow.add_conditional_edges(
            "Aggressive Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Conservative Analyst": "Conservative Analyst",
                "Risk Judge": "Risk Judge",
            },
        )
        workflow.add_conditional_edges(
            "Conservative Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Neutral Analyst": "Neutral Analyst",
                "Risk Judge": "Risk Judge",
            },
        )
        workflow.add_conditional_edges(
            "Neutral Analyst",
            self.conditional_logic.should_continue_risk_analysis,
            {
                "Aggressive Analyst": "Aggressive Analyst",
                "Risk Judge": "Risk Judge",
            },
        )

        # Phase 7 → Phase 8: 风险经理裁决 → 组合经理 → 报告生成
        workflow.add_edge("Risk Judge", "Portfolio Manager")
        workflow.add_edge("Portfolio Manager", "Report Generator")
        workflow.add_edge("Report Generator", END)
        workflow.add_edge("Circuit Breaker Pause", END)

        logger.info("[图构建] 工作流图构建完成, 开始编译")
        return workflow.compile()


def _circuit_breaker_pause_node(state: AgentState) -> dict:
    """熔断暂停节点 (PRD 10.2 极端行情)

    大盘单日跌幅 > 4% 时触发, 暂停推荐并通知用户。
    """
    from loguru import logger
    logger.warning("[熔断] 系统已暂停推荐, 触发原因: 大盘单日跌幅超过阈值 (PRD 10.2)")
    return {
        "final_portfolio": [],
        "investment_plan": "熔断暂停: 大盘跌幅超阈值, 本周期不输出推荐",
    }
