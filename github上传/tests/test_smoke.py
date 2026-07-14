"""
冒烟测试 (不连真实网络, 全 mock)

验证:
  1. 所有子包可导入
  2. 状态构造正确 (含双路选股字段)
  3. 图可编译 (用 mock LLM, 不实际 invoke)
  4. 条件路由返回值与 setup.py 节点名匹配
  5. 工具注册中心返回 8 个工具

运行: python -m pytest tests/test_smoke.py -v
"""
import os
import sys

# 确保项目根目录在 Python 路径中
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)


# =====================================================================
# Mock LLM (不实际调用, 仅满足 create_xxx(llm) 签名)
# =====================================================================
class _MockResponse:
    """模拟 LLM 响应 (含 content 属性)"""
    def __init__(self, content="{}"):
        self.content = content


class _MockLLM:
    """模拟 LLM 实例 (invoke 返回空 JSON, 不连网)"""
    def invoke(self, prompt, **kwargs):
        return _MockResponse('{"hot_topics": [], "candidates": [], "ranked_sectors": [], "policy_events": []}')

    def bind_tools(self, tools, **kwargs):
        # 返回自身 (支持链式调用)
        return self


# =====================================================================
# 测试 1: 包导入
# =====================================================================
def test_imports():
    """测试所有核心子包可导入"""
    from stock_agent.agents.utils.agent_states import AgentState, InvestDebateState, RiskDebateState
    from stock_agent.agents.utils.agent_utils import Toolkit
    from stock_agent.agents.utils.prompts import (
        NEWS_ANALYST_SYSTEM, STOCK_SELECTOR_SYSTEM, DARK_HORSE_SCANNER_SYSTEM,
    )
    from stock_agent.agents.discovery.news_analyst import create_news_analyst
    from stock_agent.agents.discovery.stock_selector import create_stock_selector
    from stock_agent.agents.discovery.dark_horse_scanner import create_dark_horse_scanner
    from stock_agent.agents.discovery.pool_merger import create_pool_merger
    from stock_agent.dataflows.providers.policy.manager import PolicySourceManager
    from stock_agent.graph.setup import GraphSetup
    from stock_agent.graph.conditional_logic import ConditionalLogic
    from stock_agent.graph.propagation import create_initial_state
    from stock_agent.tools.tool_registry import ToolRegistry

    # 验证关键符号存在
    assert AgentState is not None
    assert PolicySourceManager is not None
    assert create_pool_merger is not None


# =====================================================================
# 测试 2: 状态构造 (含双路选股字段)
# =====================================================================
def test_initial_state():
    """测试 create_initial_state 返回正确字段"""
    from stock_agent.graph.propagation import create_initial_state

    state = create_initial_state("2026-07-09")

    # 基础字段
    assert state["trade_date"] == "2026-07-09"
    assert state["user_config"]["risk_preference"] == "moderate"

    # 发现层产出字段
    assert state["hot_topics"] == []
    assert state["policy_events"] == []
    assert state["ranked_sectors"] == []
    assert state["sector_crowding_map"] == {}  # V2 新增: 板块拥挤度地图
    # 双路选股字段 (PRD 8.4)
    assert state["hot_sector_candidates"] == []
    assert state["dark_horse_candidates"] == []
    assert state["candidate_pool"] == []

    # 决策层子状态
    assert state["investment_debate_state"]["count"] == 0
    assert state["risk_debate_state"]["count"] == 0

    # 熔断标志
    assert state["circuit_breaker"] is False


# =====================================================================
# 测试 3: 图可编译 (mock LLM)
# =====================================================================
def test_graph_compiles():
    """测试 GraphSetup.setup_graph() 能成功编译 (不实际 invoke)"""
    from stock_agent.default_config import DEFAULT_CONFIG
    from stock_agent.agents.utils.agent_utils import Toolkit
    from stock_agent.graph.setup import GraphSetup
    from stock_agent.graph.conditional_logic import ConditionalLogic

    mock_llm = _MockLLM()
    toolkit = Toolkit(config=DEFAULT_CONFIG)
    conditional_logic = ConditionalLogic(
        max_debate_rounds=DEFAULT_CONFIG["max_debate_rounds"],
        max_risk_discuss_rounds=DEFAULT_CONFIG["max_risk_discuss_rounds"],
    )

    graph_setup = GraphSetup(
        quick_thinking_llm=mock_llm,
        deep_thinking_llm=mock_llm,
        toolkit=toolkit,
        conditional_logic=conditional_logic,
        config=DEFAULT_CONFIG,
    )

    # 编译图 (不 invoke, 仅验证结构正确)
    compiled = graph_setup.setup_graph()
    assert compiled is not None
    # 验证图含节点 (通过检查内部结构)
    assert hasattr(compiled, "nodes") or hasattr(compiled, "get_graph")


# =====================================================================
# 测试 4: 条件路由返回值与 setup.py 节点名匹配
# =====================================================================
def test_conditional_routing_debate():
    """测试多空辩论路由返回正确节点名"""
    from stock_agent.graph.conditional_logic import ConditionalLogic

    logic = ConditionalLogic(max_debate_rounds=2, max_risk_discuss_rounds=1)

    # 未达上限, Bull 发言后应转 Bear
    state = {
        "investment_debate_state": {
            "count": 1,
            "current_response": "Bull: 看多论点",
        }
    }
    result = logic.should_continue_debate(state)
    assert result == "Bear Researcher", f"Expected 'Bear Researcher', got '{result}'"

    # 达到上限 (2*2=4), 应转 Research Manager
    state = {
        "investment_debate_state": {
            "count": 4,
            "current_response": "Bear: 看空论点",
        }
    }
    result = logic.should_continue_debate(state)
    assert result == "Research Manager", f"Expected 'Research Manager', got '{result}'"


def test_conditional_routing_risk():
    """测试三风格风险辩论路由返回正确节点名 (与 setup.py 节点名一致)"""
    from stock_agent.graph.conditional_logic import ConditionalLogic

    logic = ConditionalLogic(max_debate_rounds=2, max_risk_discuss_rounds=1)

    # Risky 发言后 (count=1, 未达上限 3*1=3) → Conservative Analyst
    state = {
        "risk_debate_state": {
            "count": 1,
            "latest_speaker": "Risky",
        }
    }
    result = logic.should_continue_risk_analysis(state)
    assert result == "Conservative Analyst", f"Expected 'Conservative Analyst', got '{result}'"

    # Safe 发言后 (count=2) → Neutral Analyst
    state = {
        "risk_debate_state": {
            "count": 2,
            "latest_speaker": "Safe",
        }
    }
    result = logic.should_continue_risk_analysis(state)
    assert result == "Neutral Analyst", f"Expected 'Neutral Analyst', got '{result}'"

    # Neutral 发言后 (count=3, 达到上限) → Risk Judge
    state = {
        "risk_debate_state": {
            "count": 3,
            "latest_speaker": "Neutral",
        }
    }
    result = logic.should_continue_risk_analysis(state)
    assert result == "Risk Judge", f"Expected 'Risk Judge', got '{result}'"


def test_conditional_routing_analysis():
    """测试分析层入口路由"""
    from stock_agent.graph.conditional_logic import ConditionalLogic

    logic = ConditionalLogic(max_debate_rounds=2, max_risk_discuss_rounds=1)

    # 熔断 → Circuit Breaker Pause
    state = {"circuit_breaker": True, "candidate_pool": [{"ticker": "600584"}]}
    assert logic.should_run_analysis(state) == "Circuit Breaker Pause"

    # 候选池空 → Report Generator
    state = {"circuit_breaker": False, "candidate_pool": []}
    assert logic.should_run_analysis(state) == "Report Generator"

    # 正常 → Analysis Layer
    state = {"circuit_breaker": False, "candidate_pool": [{"ticker": "600584"}]}
    assert logic.should_run_analysis(state) == "Analysis Layer"


# =====================================================================
# 测试 5: 工具注册中心 (10 个工具, V2 新增 web_search + realtime_quote)
# =====================================================================
def test_tool_registry():
    """测试 ToolRegistry 返回 10 个工具"""
    from stock_agent.default_config import DEFAULT_CONFIG
    from stock_agent.tools.tool_registry import ToolRegistry

    registry = ToolRegistry(config=DEFAULT_CONFIG)
    all_tools = registry.get_all_tools()

    assert len(all_tools) == 10, f"Expected 10 tools, got {len(all_tools)}"

    # 验证各工具可单独获取
    tool_names = [
        "financial_data_query",
        "announcement_search",
        "news_search",
        "technical_indicator_calc",
        "dragon_tiger_query",
        "fund_flow_query",
        "industry_comparison",
        "research_report_search",
        "web_search",        # V2 新增
        "realtime_quote",    # V2 新增
    ]
    for name in tool_names:
        tool = registry.get_tool(name)
        assert tool is not None, f"Tool '{name}' is None"


# =====================================================================
# 测试 6: 候选池合并去重
# =====================================================================
def test_pool_merger_dedup():
    """测试 Pool Merger 去重逻辑 (主线优先)"""
    from stock_agent.agents.discovery.pool_merger import _merge_and_dedup

    hot = [
        {"ticker": "600584", "name": "长电科技", "source": "hot_sector"},
        {"ticker": "601398", "name": "工商银行", "source": "hot_sector"},
    ]
    dark = [
        {"ticker": "600584", "name": "长电科技", "source": "dark_horse"},  # 与主线重复
        {"ticker": "300750", "name": "宁德时代", "source": "dark_horse"},  # 新增
    ]

    merged = _merge_and_dedup(hot, dark)
    assert len(merged) == 3, f"Expected 3 after dedup, got {len(merged)}"

    # 主线优先: 600584 保留主线 source
    ticker_584 = next(c for c in merged if c["ticker"] == "600584")
    assert ticker_584["source"] == "hot_sector", "主线应优先保留"


# =====================================================================
# 测试 7: 政策信源管理器可实例化
# =====================================================================
def test_policy_manager_init():
    """测试 PolicySourceManager 可实例化"""
    from stock_agent.dataflows.providers.policy.manager import PolicySourceManager

    config = {"policy_sources_enabled": False}
    mgr = PolicySourceManager(config=config)

    # 未启用时应返回空数据
    result = mgr.fetch_latest(max_items=5)
    assert result["data"] == []
    assert "error" in result
