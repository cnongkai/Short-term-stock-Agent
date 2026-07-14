"""
四路并行架构集成测试 (V2)

验证板块推理重构为第四路并行节点后的架构正确性:
  1. 图结构: START → 4 路并行, 4 路 → Stock Selector + Dark Horse Scanner (fan-in)
  2. fan-in 同步: Stock Selector / Dark Horse Scanner 等待全部 4 路完成
  3. 状态字段隔离: 并行节点无写冲突 (hot_topics 用 operator.add, ranked_sectors 单写者)
  4. Sector Inference 独立性: 不依赖 News/Sentiment/Policy 输出
  5. 端到端发现层: 模拟执行发现层, 验证数据正确传递到下游
"""
import operator
import time
from typing import Annotated
from typing_extensions import TypedDict
from unittest.mock import patch, MagicMock

import pytest
from langgraph.graph import END, StateGraph, START


# 合成历史板块分数 (模拟 _load_prev_scores 返回, 供 Sector Inference 历史兜底)
# V2: sector_inference 数据源失败时降级到历史均值 (替代已删除的 _get_static_sectors)
_FAKE_PREV_SCORES = {
    "半导体": {"change_pct": 3.0, "fund_flow": 20e8, "turnover": 4.0,
              "up_count": 90, "down_count": 10, "components": ["600584"],
              "total_score": 60.0},
    "医药": {"change_pct": -2.0, "fund_flow": -5e8, "turnover": 2.0,
            "up_count": 20, "down_count": 80, "components": [],
            "total_score": 20.0},
    "新能源": {"change_pct": 2.5, "fund_flow": 15e8, "turnover": 5.0,
              "up_count": 75, "down_count": 25, "components": ["300274"],
              "total_score": 55.0},
}


# =====================================================================
# 测试 1: 图结构验证 (四路并行 + fan-in)
# =====================================================================
class TestGraphStructure:
    """验证 setup.py 构建的图具有正确的四路并行结构"""

    def _build_graph(self):
        """构建真实图 (使用 Mock LLM)"""
        from stock_agent.default_config import DEFAULT_CONFIG
        from stock_agent.agents.utils.agent_utils import Toolkit
        from stock_agent.graph.setup import GraphSetup
        from stock_agent.graph.conditional_logic import ConditionalLogic

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content='{"hot_topics":[],"candidates":[],"policy_events":[]}')

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
        return graph_setup.setup_graph()

    def test_graph_compiles(self):
        """图能成功编译"""
        compiled = self._build_graph()
        assert compiled is not None

    def test_sector_inference_is_parallel_from_start(self):
        """Sector Inference 从 START 出发 (与三路分析师并行, 非下游)"""
        compiled = self._build_graph()
        graph_data = compiled.get_graph()

        # 获取 START 的后继节点
        start_successors = set()
        for edge in graph_data.edges:
            if edge[0] == "__start__":
                start_successors.add(edge[1])

        assert "News Analyst" in start_successors
        assert "Sentiment Analyst" in start_successors
        assert "Policy Analyst" in start_successors
        assert "Sector Inference" in start_successors, "Sector Inference 应从 START 出发 (第四路并行)"

    def test_sector_inference_not_downstream_of_analysts(self):
        """Sector Inference 不是三路分析师的下游 (无 News→Sector 等边)"""
        compiled = self._build_graph()
        graph_data = compiled.get_graph()

        for edge in graph_data.edges:
            src, dst = edge[0], edge[1]
            if dst == "Sector Inference" and src != "__start__":
                pytest.fail(
                    f"Sector Inference 不应有前驱 {src} (应仅从 START 出发), "
                    f"发现边: {src} → {dst}"
                )

    def test_fan_in_to_stock_selector(self):
        """四路并行节点都连向 Stock Selector (fan-in 同步)"""
        compiled = self._build_graph()
        graph_data = compiled.get_graph()

        stock_selector_preds = set()
        for edge in graph_data.edges:
            if edge[1] == "Stock Selector":
                stock_selector_preds.add(edge[0])

        expected = {"News Analyst", "Sentiment Analyst", "Policy Analyst", "Sector Inference"}
        assert stock_selector_preds == expected, (
            f"Stock Selector 前驱应为四路并行节点, 实际: {stock_selector_preds}"
        )

    def test_fan_in_to_dark_horse_scanner(self):
        """四路并行节点都连向 Dark Horse Scanner (fan-in 同步)"""
        compiled = self._build_graph()
        graph_data = compiled.get_graph()

        dark_horse_preds = set()
        for edge in graph_data.edges:
            if edge[1] == "Dark Horse Scanner":
                dark_horse_preds.add(edge[0])

        expected = {"News Analyst", "Sentiment Analyst", "Policy Analyst", "Sector Inference"}
        assert dark_horse_preds == expected, (
            f"Dark Horse Scanner 前驱应为四路并行节点, 实际: {dark_horse_preds}"
        )

    def test_no_edge_from_analysts_to_sector_inference(self):
        """三路分析师不再连向 Sector Inference (旧架构已废弃)"""
        compiled = self._build_graph()
        graph_data = compiled.get_graph()

        for edge in graph_data.edges:
            src, dst = edge[0], edge[1]
            if dst == "Sector Inference" and src in ("News Analyst", "Sentiment Analyst", "Policy Analyst"):
                pytest.fail(f"发现旧架构残留边: {src} → Sector Inference")


# =====================================================================
# 测试 2: fan-in 同步 (用最小化 StateGraph 验证 LangGraph 语义)
# =====================================================================
class _ParallelState(TypedDict):
    """最小化测试状态 (模拟发现层并行)"""
    hot_topics: Annotated[list, operator.add]
    ranked_sectors: Annotated[list, "板块排名"]
    sector_crowding_map: Annotated[dict, "拥挤度地图"]
    hot_sector_candidates: Annotated[list, "主线候选"]
    dark_horse_candidates: Annotated[list, "黑马候选"]
    candidate_pool: Annotated[list, "候选池"]
    execution_log: Annotated[list, operator.add]


class TestFanInSynchronization:
    """验证 LangGraph fan-in 语义: 目标节点等待所有前驱完成"""

    def test_stock_selector_receives_all_parallel_outputs(self):
        """Stock Selector 能读取全部 4 路并行节点的产出"""
        execution_order = []

        def news_node(state):
            execution_order.append("news")
            time.sleep(0.05)  # 模拟耗时
            return {"hot_topics": [{"topic": "新闻热点"}], "execution_log": ["news_done"]}

        def sentiment_node(state):
            execution_order.append("sentiment")
            return {"hot_topics": [{"topic": "情绪热点"}], "execution_log": ["sentiment_done"]}

        def policy_node(state):
            execution_order.append("policy")
            return {"hot_topics": [{"topic": "政策热点"}], "execution_log": ["policy_done"]}

        def sector_node(state):
            execution_order.append("sector")
            time.sleep(0.1)  # Sector Inference 最慢
            return {
                "ranked_sectors": [{"sector": "半导体", "heat_score": 75}],
                "sector_crowding_map": {"top_10": []},
                "execution_log": ["sector_done"],
            }

        captured_state = {}

        def stock_selector_node(state):
            # fan-in: 此时所有 4 路应已完成
            captured_state["hot_topics"] = state.get("hot_topics", [])
            captured_state["ranked_sectors"] = state.get("ranked_sectors", [])
            captured_state["sector_crowding_map"] = state.get("sector_crowding_map", {})
            execution_order.append("stock_selector")
            return {"hot_sector_candidates": [{"ticker": "600584"}], "execution_log": ["selector_done"]}

        def dark_horse_node(state):
            captured_state["dark_horse_hot_topics"] = state.get("hot_topics", [])
            execution_order.append("dark_horse")
            return {"dark_horse_candidates": [], "execution_log": ["dark_done"]}

        def merger_node(state):
            execution_order.append("merger")
            return {"candidate_pool": state.get("hot_sector_candidates", [])}

        workflow = StateGraph(_ParallelState)
        workflow.add_node("News", news_node)
        workflow.add_node("Sentiment", sentiment_node)
        workflow.add_node("Policy", policy_node)
        workflow.add_node("Sector", sector_node)
        workflow.add_node("StockSelector", stock_selector_node)
        workflow.add_node("DarkHorse", dark_horse_node)
        workflow.add_node("Merger", merger_node)

        # 四路并行
        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(START, n)
        # fan-in 到双路选股
        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(n, "StockSelector")
            workflow.add_edge(n, "DarkHorse")
        # 合并
        workflow.add_edge("StockSelector", "Merger")
        workflow.add_edge("DarkHorse", "Merger")
        workflow.add_edge("Merger", END)

        compiled = workflow.compile()
        result = compiled.invoke({})

        # Stock Selector 应收到全部 3 路 hot_topics (operator.add 累积)
        assert len(captured_state["hot_topics"]) == 3, (
            f"fan-in 失败: Stock Selector 应收到 3 条 hot_topics, "
            f"实际 {len(captured_state['hot_topics'])}: {captured_state['hot_topics']}"
        )
        # Stock Selector 应收到 Sector Inference 的 ranked_sectors
        assert len(captured_state["ranked_sectors"]) == 1
        assert captured_state["ranked_sectors"][0]["sector"] == "半导体"
        # Sector Inference 的 crowding_map 也应传递
        assert "top_10" in captured_state["sector_crowding_map"]
        # Dark Horse Scanner 同样收到全部 hot_topics
        assert len(captured_state["dark_horse_hot_topics"]) == 3
        # Merger 在最后
        assert execution_order[-1] == "merger"

    def test_parallel_nodes_run_before_fan_in_targets(self):
        """所有 4 路并行节点都在 StockSelector / DarkHorse 之前完成"""
        execution_order = []

        def make_node(name, delay=0):
            def node(state):
                execution_order.append(f"{name}_start")
                if delay:
                    time.sleep(delay)
                execution_order.append(f"{name}_end")
                return {"hot_topics": [], "execution_log": [name]}
            return node

        workflow = StateGraph(_ParallelState)
        workflow.add_node("News", make_node("news", 0.05))
        workflow.add_node("Sentiment", make_node("sentiment"))
        workflow.add_node("Policy", make_node("policy"))
        workflow.add_node("Sector", make_node("sector", 0.1))

        def selector(state):
            execution_order.append("selector")
            return {"hot_sector_candidates": []}

        def dark_horse(state):
            execution_order.append("dark_horse")
            return {"dark_horse_candidates": []}

        def merger(state):
            execution_order.append("merger")
            return {"candidate_pool": []}

        workflow.add_node("Selector", selector)
        workflow.add_node("DarkHorse", dark_horse)
        workflow.add_node("Merger", merger)

        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(START, n)
        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(n, "Selector")
            workflow.add_edge(n, "DarkHorse")
        workflow.add_edge("Selector", "Merger")
        workflow.add_edge("DarkHorse", "Merger")
        workflow.add_edge("Merger", END)

        compiled = workflow.compile()
        compiled.invoke({})

        # 所有 4 路的 _end 都在 selector / dark_horse 之前
        parallel_ends = [i for i, e in enumerate(execution_order) if e.endswith("_end")]
        fan_in_starts = [
            i for i, e in enumerate(execution_order)
            if e in ("selector", "dark_horse")
        ]
        assert parallel_ends, "应有并行节点完成记录"
        assert fan_in_starts, "应有 fan-in 节点启动记录"
        assert max(parallel_ends) < min(fan_in_starts), (
            f"并行节点未全部完成就启动 fan-in: "
            f"并行完成位置 {parallel_ends}, fan-in 启动位置 {fan_in_starts}, "
            f"顺序: {execution_order}"
        )


# =====================================================================
# 测试 3: 状态字段隔离 (无写冲突)
# =====================================================================
class TestStateFieldIsolation:
    """验证并行节点使用隔离的状态字段, 不会产生写冲突"""

    def test_hot_topics_uses_operator_add(self):
        """hot_topics 使用 operator.add reducer, 三路并行安全拼接"""
        from stock_agent.agents.utils.agent_states import AgentState
        import operator as op

        # 检查 AgentState 中 hot_topics 的 reducer
        hints = AgentState.__annotations__
        # Annotated[list, operator.add] 的第二个元素应是 operator.add
        hot_topics_meta = hints.get("hot_topics")
        assert hot_topics_meta is not None
        # Annotated 类型: __metadata__ 是 tuple
        if hasattr(hot_topics_meta, "__metadata__"):
            reducer = hot_topics_meta.__metadata__[0]
            assert reducer is op.add, f"hot_topics reducer 应为 operator.add, 实际: {reducer}"

    def test_ranked_sectors_single_writer(self):
        """ranked_sectors 是单写者 (仅 Sector Inference 写, 无 reducer)"""
        from stock_agent.agents.utils.agent_states import AgentState

        hints = AgentState.__annotations__
        ranked_meta = hints.get("ranked_sectors")
        assert ranked_meta is not None
        # 单写者: metadata 应为字符串描述, 非 operator.add
        if hasattr(ranked_meta, "__metadata__"):
            reducer = ranked_meta.__metadata__[0]
            assert reducer is not operator.add, (
                "ranked_sectors 不应使用 operator.add (单写者模式)"
            )

    def test_sector_crowding_map_single_writer(self):
        """sector_crowding_map 是单写者 (仅 Sector Inference 写)"""
        from stock_agent.agents.utils.agent_states import AgentState

        hints = AgentState.__annotations__
        map_meta = hints.get("sector_crowding_map")
        assert map_meta is not None
        if hasattr(map_meta, "__metadata__"):
            reducer = map_meta.__metadata__[0]
            assert reducer is not operator.add, (
                "sector_crowding_map 不应使用 operator.add (单写者模式)"
            )

    def test_hot_sector_candidates_single_writer(self):
        """hot_sector_candidates 是单写者 (仅 Stock Selector 写)"""
        from stock_agent.agents.utils.agent_states import AgentState

        hints = AgentState.__annotations__
        meta = hints.get("hot_sector_candidates")
        assert meta is not None
        if hasattr(meta, "__metadata__"):
            reducer = meta.__metadata__[0]
            assert reducer is not operator.add

    def test_dark_horse_candidates_single_writer(self):
        """dark_horse_candidates 是单写者 (仅 Dark Horse Scanner 写)"""
        from stock_agent.agents.utils.agent_states import AgentState

        hints = AgentState.__annotations__
        meta = hints.get("dark_horse_candidates")
        assert meta is not None
        if hasattr(meta, "__metadata__"):
            reducer = meta.__metadata__[0]
            assert reducer is not operator.add


# =====================================================================
# 测试 4: Sector Inference 独立性
# =====================================================================
class TestSectorInferenceIndependence:
    """验证 Sector Inference 不依赖 News/Sentiment/Policy 的输出"""

    def test_sector_inference_ignores_hot_topics(self):
        """Sector Inference 节点不读取 hot_topics / policy_events"""
        from stock_agent.agents.discovery.sector_inference import create_sector_inference
        import inspect

        node = create_sector_inference(llm=None, config={"sector_top_n": 5})
        source = inspect.getsource(node)

        # Sector Inference 不应读取 hot_topics 或 policy_events
        assert "hot_topics" not in source, "Sector Inference 不应依赖 hot_topics"
        assert "policy_events" not in source, "Sector Inference 不应依赖 policy_events"

    def test_sector_inference_works_with_empty_analyst_output(self):
        """三路分析师产出为空时, Sector Inference 仍正常工作"""
        from stock_agent.agents.discovery.sector_inference import create_sector_inference

        # 模拟三路分析师全部返回空
        state = {
            "trade_date": "2026-07-11",
            "hot_topics": [],        # 三路分析师无产出
            "policy_events": [],     # 政策分析师无产出
        }

        with patch.object(si_module(), "_fetch_sector_data_raw", return_value=None):
            with patch.object(si_module(), "_fetch_attention_scores", return_value={}):
                with patch.object(si_module(), "_load_prev_scores", return_value=_FAKE_PREV_SCORES):
                    with patch.object(si_module(), "_save_scores"):
                        node = create_sector_inference(llm=None, config={"sector_top_n": 5})
                        result = node(state)

        # 即使三路分析师无产出, Sector Inference 仍应返回 ranked_sectors
        assert len(result["ranked_sectors"]) > 0, (
            "Sector Inference 应独立工作, 不依赖三路分析师产出"
        )
        assert "sector_crowding_map" in result

    def test_sector_inference_reads_only_trade_date_and_config(self):
        """Sector Inference 仅读取 trade_date (不读分析师产出)"""
        from stock_agent.agents.discovery.sector_inference import create_sector_inference
        import inspect

        node = create_sector_inference(llm=None, config={"sector_top_n": 5})
        source = inspect.getsource(node)

        # 应读取 trade_date
        assert "trade_date" in source
        # 不应读取分析师产出的状态字段
        for field in ["hot_topics", "policy_events", "market_sentiment"]:
            assert field not in source, f"Sector Inference 不应读取 {field}"


def si_module():
    """获取 sector_inference 模块 (便于 patch)"""
    from stock_agent.agents.discovery import sector_inference
    return sector_inference


# =====================================================================
# 测试 5: 端到端发现层模拟执行
# =====================================================================
class TestDiscoveryLayerIntegration:
    """模拟执行发现层, 验证四路并行数据正确传递到双路选股"""

    def test_discovery_layer_data_flow(self):
        """四路并行产出 → fan-in → Stock Selector + Dark Horse Scanner 收到完整数据"""
        from stock_agent.agents.discovery.sector_inference import create_sector_inference

        captured = {}

        # === Mock 节点 (模拟四路并行 + 双路选股 + 合并) ===
        def mock_news(state):
            return {"hot_topics": [{"topic": "半导体涨价", "source_type": "news"}]}

        def mock_sentiment(state):
            return {"hot_topics": [{"topic": "股吧热议AI", "source_type": "social"}]}

        def mock_policy(state):
            return {"hot_topics": [{"topic": "芯片政策", "source_type": "policy"}],
                    "policy_events": [{"title": "国务院芯片扶持"}]}

        # Sector Inference 用真实实现 (mock 数据源)
        sector_node_fn = create_sector_inference(llm=None, config={"sector_top_n": 5})

        def stock_selector(state):
            captured["selector_hot_topics"] = state.get("hot_topics", [])
            captured["selector_ranked_sectors"] = state.get("ranked_sectors", [])
            captured["selector_crowding_map"] = state.get("sector_crowding_map", {})
            return {"hot_sector_candidates": [{"ticker": "600584", "source": "hot_sector"}]}

        def dark_horse(state):
            captured["dark_horse_hot_topics"] = state.get("hot_topics", [])
            captured["dark_horse_ranked_sectors"] = state.get("ranked_sectors", [])
            return {"dark_horse_candidates": []}

        def merger(state):
            hot = state.get("hot_sector_candidates", [])
            dark = state.get("dark_horse_candidates", [])
            return {"candidate_pool": hot + dark}

        # === 构建最小化发现层图 ===
        workflow = StateGraph(_ParallelState)
        workflow.add_node("News", mock_news)
        workflow.add_node("Sentiment", mock_sentiment)
        workflow.add_node("Policy", mock_policy)
        workflow.add_node("Sector", sector_node_fn)
        workflow.add_node("StockSelector", stock_selector)
        workflow.add_node("DarkHorse", dark_horse)
        workflow.add_node("Merger", merger)

        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(START, n)
        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(n, "StockSelector")
            workflow.add_edge(n, "DarkHorse")
        workflow.add_edge("StockSelector", "Merger")
        workflow.add_edge("DarkHorse", "Merger")
        workflow.add_edge("Merger", END)

        compiled = workflow.compile()

        # === Mock Sector Inference 的外部依赖 ===
        with patch.object(si_module(), "_fetch_sector_data_raw", return_value=None):
            with patch.object(si_module(), "_fetch_attention_scores", return_value={}):
                with patch.object(si_module(), "_load_prev_scores", return_value=_FAKE_PREV_SCORES):
                    with patch.object(si_module(), "_save_scores"):
                        result = compiled.invoke({})

        # === 验证 fan-in 数据传递 ===
        # 1. Stock Selector 收到全部 3 路 hot_topics
        assert len(captured["selector_hot_topics"]) == 3, (
            f"Stock Selector 应收到 3 条 hot_topics (三路分析师), "
            f"实际: {len(captured['selector_hot_topics'])}"
        )
        # 2. Stock Selector 收到 Sector Inference 的 ranked_sectors
        assert len(captured["selector_ranked_sectors"]) > 0, (
            "Stock Selector 应收到 Sector Inference 的 ranked_sectors"
        )
        # 3. Stock Selector 收到 sector_crowding_map
        assert "top_10" in captured["selector_crowding_map"], (
            "Stock Selector 应收到 sector_crowding_map"
        )
        # 4. Dark Horse Scanner 同样收到完整数据
        assert len(captured["dark_horse_hot_topics"]) == 3
        assert len(captured["dark_horse_ranked_sectors"]) > 0
        # 5. 最终候选池包含 Stock Selector 的产出
        assert len(result["candidate_pool"]) == 1
        assert result["candidate_pool"][0]["ticker"] == "600584"

    def test_no_concurrent_update_error(self):
        """四路并行执行不触发 ConcurrentUpdateError (状态字段隔离正确)"""
        # 这个测试验证: 并行写入不同状态字段不会报错
        from stock_agent.agents.discovery.sector_inference import create_sector_inference

        workflow = StateGraph(_ParallelState)

        def news(state):
            return {"hot_topics": [{"t": 1}]}
        def sentiment(state):
            return {"hot_topics": [{"t": 2}]}
        def policy(state):
            return {"hot_topics": [{"t": 3}], "policy_events": [{"p": 1}]}
        sector_fn = create_sector_inference(llm=None, config={"sector_top_n": 5})

        def selector(state):
            return {"hot_sector_candidates": [{"ticker": "001"}]}
        def dark_horse(state):
            return {"dark_horse_candidates": [{"ticker": "002"}]}
        def merger(state):
            hot = state.get("hot_sector_candidates", [])
            dark = state.get("dark_horse_candidates", [])
            return {"candidate_pool": hot + dark}

        workflow.add_node("News", news)
        workflow.add_node("Sentiment", sentiment)
        workflow.add_node("Policy", policy)
        workflow.add_node("Sector", sector_fn)
        workflow.add_node("Selector", selector)
        workflow.add_node("DarkHorse", dark_horse)
        workflow.add_node("Merger", merger)

        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(START, n)
        for n in ["News", "Sentiment", "Policy", "Sector"]:
            workflow.add_edge(n, "Selector")
            workflow.add_edge(n, "DarkHorse")
        workflow.add_edge("Selector", "Merger")
        workflow.add_edge("DarkHorse", "Merger")
        workflow.add_edge("Merger", END)

        compiled = workflow.compile()

        with patch.object(si_module(), "_fetch_sector_data_raw", return_value=None):
            with patch.object(si_module(), "_fetch_attention_scores", return_value={}):
                with patch.object(si_module(), "_load_prev_scores", return_value=_FAKE_PREV_SCORES):
                    with patch.object(si_module(), "_save_scores"):
                        # 不应抛出 ConcurrentUpdateError
                        result = compiled.invoke({})

        # hot_topics 应正确累积 (operator.add)
        assert len(result["hot_topics"]) == 3
        # candidate_pool 应包含双路选股结果
        assert len(result["candidate_pool"]) == 2
