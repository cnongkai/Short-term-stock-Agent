"""
短线推荐 Agent 主图类 (对应 PRD 第6章核心流程设计)

StockRecommendationGraph 是系统主入口, 负责:
  1. 初始化 LLM (quick/deep, 默认 DeepSeek)
  2. 初始化 Toolkit (8 工具)
  3. 构建 LangGraph 工作流图
  4. 提供 propagate() 方法执行全流程

参考架构: TradingAgents-CN graph/trading_graph.py (TradingAgentsGraph)
"""
import os
import json
from typing import Dict, Any, Tuple, Optional

from loguru import logger

from stock_agent.default_config import DEFAULT_CONFIG
from stock_agent.llm_clients import create_llm_client
from stock_agent.agents.utils.agent_utils import Toolkit
from stock_agent.graph.setup import GraphSetup
from stock_agent.graph.conditional_logic import ConditionalLogic
from stock_agent.graph.propagation import create_initial_state, get_graph_args
from stock_agent.graph.run_metrics import get_metrics, generate_verification_report


def _create_llm(provider: str, model: str, base_url: str, api_key: str,
                temperature: float, max_tokens: int, timeout: int):
    """创建 LLM 实例 (统一入口, 走 OpenAI 兼容协议)"""
    logger.info(f"[LLM 初始化] provider={provider}, model={model}, url={base_url}")
    client = create_llm_client(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return client.get_llm()


class StockRecommendationGraph:
    """短线推荐 Agent 主图类

    编排发现层 → 分析层 → 决策层 → 输出层的完整流程。
    """

    def __init__(self, config: Dict[str, Any] = None, debug: bool = False):
        """初始化图与组件

        Args:
            config: 配置字典 (None 则用 DEFAULT_CONFIG)
            debug: 调试模式
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG

        # === 初始化 LLM (PRD Phase 0) ===
        provider = self.config.get("llm_provider", "deepseek")
        backend_url = self.config.get("backend_url", "https://api.deepseek.com")

        # 从 .env 读取 API Key
        from config.settings import get_api_key
        api_key = get_api_key(provider)

        if not api_key:
            raise ValueError(
                f"未找到 {provider} 的 API Key, 请在 .env 文件中设置 "
                f"(如 DEEPSEEK_API_KEY=sk-xxx)"
            )

        quick_cfg = self.config.get("quick_model_config", {})
        deep_cfg = self.config.get("deep_model_config", {})

        # quick 与 deep 可使用相同模型 (DeepSeek)
        self.quick_thinking_llm = _create_llm(
            provider=provider,
            model=self.config["quick_think_llm"],
            base_url=backend_url,
            api_key=api_key,
            temperature=quick_cfg.get("temperature", 0.7),
            max_tokens=quick_cfg.get("max_tokens", 4000),
            timeout=quick_cfg.get("timeout", 180),
        )

        self.deep_thinking_llm = _create_llm(
            provider=provider,
            model=self.config["deep_think_llm"],
            base_url=backend_url,
            api_key=api_key,
            temperature=deep_cfg.get("temperature", 0.7),
            max_tokens=deep_cfg.get("max_tokens", 4000),
            timeout=deep_cfg.get("timeout", 180),
        )

        logger.info("[LLM 初始化] quick/deep LLM 创建成功")

        # === 初始化 Toolkit (PRD M4c) ===
        self.toolkit = Toolkit(config=self.config)

        # === 初始化条件路由 ===
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config.get("max_debate_rounds", 2),
            max_risk_discuss_rounds=self.config.get("max_risk_discuss_rounds", 1),
        )

        # === 构建工作流图 ===
        graph_setup = GraphSetup(
            quick_thinking_llm=self.quick_thinking_llm,
            deep_thinking_llm=self.deep_thinking_llm,
            toolkit=self.toolkit,
            conditional_logic=self.conditional_logic,
            config=self.config,
        )
        self.graph = graph_setup.setup_graph()
        logger.info("[图构建] StockRecommendationGraph 初始化完成")

    def propagate(
        self,
        trade_date: str,
        user_config: Dict[str, Any] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """执行全流程推荐

        Args:
            trade_date: 交易日期 (YYYY-MM-DD)
            user_config: 用户配置 (PRD M9)

        Returns:
            (final_state, final_decision): 最终状态字典 + 决策摘要
        """
        logger.info(f"{'=' * 60}")
        logger.info(f"[开始推荐] 交易日期: {trade_date}")
        logger.info(f"{'=' * 60}")

        # 启动运行指标收集 (参考 4级深度分析验证报告 指标体系)
        metrics = get_metrics()
        metrics.start(trade_date)

        initial_state = create_initial_state(trade_date, user_config)
        graph_args = get_graph_args(self.config)

        # 执行图
        try:
            final_state = self.graph.invoke(
                initial_state,
                config={"recursion_limit": graph_args["max_recur_limit"]},
            )
        finally:
            # 无论成功失败都结束计时
            metrics.finish()

        # 输出结果
        decision = final_state.get("final_trade_decision", "")
        portfolio = final_state.get("final_portfolio", [])

        logger.info(f"[推荐完成] 推荐标的数: {len(portfolio)}")

        # 保存全链路日志 (PRD: 全链路留痕)
        self._save_results(final_state, trade_date)

        # 生成验证报告 (参考 4级深度分析验证报告 格式, Task #6)
        try:
            reports_dir = os.path.join(
                self.config.get("results_dir", "./results"), "reports"
            )
            report_path = generate_verification_report(
                metrics, final_state, self.config, save_dir=reports_dir
            )
            logger.info(f"[验证报告] 已生成: {report_path}")
        except Exception as e:
            logger.error(f"[验证报告] 生成失败: {e}")

        return final_state, decision

    def _save_results(self, state: Dict[str, Any], trade_date: str) -> None:
        """保存结果到文件 (PRD Phase 8 全链路留痕)"""
        results_dir = self.config.get("results_dir", "./results")
        log_dir = self.config.get("log_dir", "./results/logs")
        os.makedirs(results_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)

        # 保存完整状态日志 (JSON)
        log_path = os.path.join(log_dir, f"full_states_log_{trade_date}.json")
        try:
            # 过滤不可序列化的 messages
            serializable_state = {
                k: v for k, v in state.items()
                if k != "messages" and _is_json_serializable(v)
            }
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(serializable_state, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"[结果保存] 全链路日志: {log_path}")
        except Exception as e:
            logger.error(f"[结果保存] 保存日志失败: {e}")

        # 推荐报告由 report_generator 节点生成并写入 results/


def _is_json_serializable(obj: Any) -> bool:
    """检查对象是否可 JSON 序列化"""
    try:
        json.dumps(obj, default=str)
        return True
    except (TypeError, ValueError):
        return False
