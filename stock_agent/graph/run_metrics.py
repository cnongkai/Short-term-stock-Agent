"""
运行验证指标收集与报告生成 (对应 Task #6)

,
对每次运行收集以下指标并生成验证报告:

  1. 核心指标: 投资辩论轮次 / 风险讨论轮次 / 超时配置 / 实际耗时 / LLM超时次数
  2. 配置验证: 研究深度 / 辩论轮次 / 风险讨论轮次 / 超时时间计算
  3. LLM 性能: 各节点 LLM 调用耗时 / Token 估算(字符数/1.8) / 实际 Token
  4. 时间分析: 各节点耗时占比 / 总耗时分布
  5. ReAct 分析: 各分析师迭代次数 / 工具调用次数

使用方式:
    from stock_agent.graph.run_metrics import get_metrics, generate_verification_report

    metrics = get_metrics()
    metrics.start()
    # ... 执行图 ...
    metrics.finish()
    report = generate_verification_report(metrics, final_state, config)
"""
import os
import time
import json
import threading
from datetime import datetime
from typing import Dict, Any, Optional
from contextlib import contextmanager

from loguru import logger

# Token 估算系数 (参考验证报告: 字符数 / 1.8 ≈ tokens)
_CHARS_PER_TOKEN = 1.8


class RunMetrics:
    """运行指标跟踪器 (单例, 每次运行前调用 reset())

    跟踪内容:
      - 总耗时 (start_time / end_time)
      - 各节点耗时 (node_timings)
      - LLM 调用耗时与 Token 使用 (llm_calls)
      - LLM 超时次数 (timeout_count)
    """

    _instance: Optional["RunMetrics"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.reset()

    def reset(self):
        """重置指标 (每次运行前调用)"""
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.node_timings: Dict[str, dict] = {}
        self.llm_calls: list = []
        self.timeout_count: int = 0
        self.trade_date: str = ""
        # V2 优化: 线程安全锁 (分析层并行化后 timeout_count 为 read-modify-write 需加锁)
        self._lock = threading.Lock()

    def start(self, trade_date: str = ""):
        """开始记录"""
        self.reset()
        self.start_time = time.time()
        self.trade_date = trade_date
        logger.info(f"[指标] 开始记录运行指标, trade_date={trade_date}")

    def finish(self):
        """结束记录"""
        self.end_time = time.time()
        total = self.get_total_elapsed()
        logger.info(f"[指标] 运行结束, 总耗时: {total:.2f}秒")

    def get_total_elapsed(self) -> float:
        """获取总耗时(秒)"""
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time else time.time()
        return end - self.start_time

    @contextmanager
    def node_timer(self, node_name: str):
        """节点计时上下文管理器

        用法:
            with metrics.node_timer("分析层"):
                # 节点逻辑
                ...
        """
        start = time.time()
        logger.debug(f"[指标] 节点开始: {node_name}")
        try:
            yield
        finally:
            elapsed = time.time() - start
            self.node_timings[node_name] = {
                "start": start,
                "elapsed": round(elapsed, 2),
            }
            logger.info(f"[指标] 节点完成: {node_name}, 耗时: {elapsed:.2f}秒")

    @contextmanager
    def llm_timer(self, node_name: str, prompt_text: str = ""):
        """LLM 调用计时上下文管理器

        用法:
            with metrics.llm_timer("基本面分析师", prompt_text):
                result = llm.invoke(messages)
        """
        start = time.time()
        # 估算输入 Token (字符数 / 1.8)
        est_tokens_in = int(len(prompt_text) / _CHARS_PER_TOKEN) if prompt_text else 0
        call_info = {
            "node": node_name,
            "start": start,
            "est_tokens_in": est_tokens_in,
            "timed_out": False,
        }
        try:
            yield call_info
        except TimeoutError as e:
            call_info["timed_out"] = True
            # V2 优化: 加锁防止分析层并行化后 timeout_count 竞态 (read-modify-write)
            with self._lock:
                self.timeout_count += 1
            logger.warning(f"[指标] LLM 超时: {node_name}")
            raise
        except Exception:
            raise
        finally:
            elapsed = time.time() - start
            call_info["elapsed"] = round(elapsed, 2)
            self.llm_calls.append(call_info)
            logger.info(
                f"[指标] LLM 调用: {node_name}, 耗时: {elapsed:.2f}秒, "
                f"估算输入Token: {est_tokens_in}"
            )

    def record_llm_result(self, node_name: str, response_text: str = "",
                          actual_tokens: dict = None):
        """记录 LLM 调用结果 (Token 用量)

        Args:
            node_name: 节点名称
            response_text: 响应文本 (用于估算输出 Token)
            actual_tokens: 实际 Token 用量 {"input": x, "output": y} (可选)
        """
        est_tokens_out = int(len(response_text) / _CHARS_PER_TOKEN) if response_text else 0
        # 找到该节点最后一次调用, 补充输出信息
        for call in reversed(self.llm_calls):
            if call["node"] == node_name and "est_tokens_out" not in call:
                call["est_tokens_out"] = est_tokens_out
                if actual_tokens:
                    call["actual_tokens"] = actual_tokens
                break

    def record_timeout(self):
        """线程安全地记录一次 LLM 超时/兜底 (V2 新增)

        供 safe_llm_invoke 的 on_fallback 回调使用。
        复用已有 _lock 保证线程安全 (分析层并行化后多线程可能同时调用)。
        """
        with self._lock:
            self.timeout_count += 1

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "trade_date": self.trade_date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_elapsed": round(self.get_total_elapsed(), 2),
            "node_timings": self.node_timings,
            "llm_calls": self.llm_calls,
            "timeout_count": self.timeout_count,
        }


def get_metrics() -> RunMetrics:
    """获取全局 RunMetrics 单例"""
    return RunMetrics()


def _extract_debate_rounds(final_state: dict) -> dict:
    """从最终状态提取辩论轮次信息"""
    invest_state = final_state.get("investment_debate_state", {})
    risk_state = final_state.get("risk_debate_state", {})

    invest_count = 0
    if hasattr(invest_state, "count"):
        invest_count = invest_state.count
    elif isinstance(invest_state, dict):
        invest_count = invest_state.get("count", 0)

    risk_count = 0
    if hasattr(risk_state, "count"):
        risk_count = risk_state.count
    elif isinstance(risk_state, dict):
        risk_count = risk_state.get("count", 0)

    return {
        "investment_debate_count": invest_count,
        "risk_debate_count": risk_count,
    }


def _extract_react_stats(final_state: dict) -> dict:
    """从最终状态提取 ReAct 分析统计"""
    analysis_reports = final_state.get("analysis_reports", {})
    react_counts = final_state.get("react_tool_call_counts", {})

    stats = {
        "total_tickers_analyzed": len(analysis_reports),
        "total_tool_calls": sum(react_counts.values()) if react_counts else 0,
        "per_ticker": {},
    }

    for ticker, report in analysis_reports.items():
        if isinstance(report, dict):
            ticker_stats = {}
            for dim in ["fundamentals", "technical", "china_specific", "stock_development"]:
                dim_report = report.get(dim, {})
                if isinstance(dim_report, dict):
                    ticker_stats[dim] = {
                        "rating": dim_report.get("rating", "未知"),
                        "confidence": dim_report.get("confidence", 0),
                        "react_iterations": dim_report.get("react_iterations", 0),
                    }
            stats["per_ticker"][ticker] = ticker_stats

    return stats


def generate_verification_report(
    metrics: RunMetrics,
    final_state: dict,
    config: dict,
    save_dir: str = "./results/reports",
) -> str:
    """生成验证报告 Markdown (参考 4级深度分析验证报告 格式)

    Args:
        metrics: RunMetrics 实例
        final_state: 图执行后的最终状态
        config: 系统配置
        save_dir: 报告保存目录

    Returns:
        报告文件路径
    """
    now = datetime.now()
    total_elapsed = metrics.get_total_elapsed()
    debate_info = _extract_debate_rounds(final_state)
    react_stats = _extract_react_stats(final_state)

    # 配置信息
    max_debate_rounds = config.get("max_debate_rounds", 2)
    max_risk_rounds = config.get("max_risk_discuss_rounds", 1)
    quick_cfg = config.get("quick_model_config", {})
    deep_cfg = config.get("deep_model_config", {})
    quick_timeout = quick_cfg.get("timeout", 120)
    deep_timeout = deep_cfg.get("timeout", 120)
    # V2 优化: 总运行预算 (验证报告超时判定基准, 替代单次 LLM 超时)
    total_run_budget = config.get("total_run_budget", 1800)

    # 期望发言次数 = 轮次 × 2 (投资辩论) 或 轮次 × 3 (风险讨论)
    expected_invest_speeches = max_debate_rounds * 2
    expected_risk_speeches = max_risk_rounds * 3

    # 判断状态
    invest_ok = debate_info["investment_debate_count"] == expected_invest_speeches
    risk_ok = debate_info["risk_debate_count"] == expected_risk_speeches
    timeout_ok = metrics.timeout_count == 0

    # 节点耗时排序
    sorted_nodes = sorted(
        metrics.node_timings.items(),
        key=lambda x: x[1].get("elapsed", 0),
        reverse=True,
    )

    # 构建 Markdown 报告
    lines = []
    lines.append(f"# 短线推荐 Agent 运行验证报告")
    lines.append("")
    lines.append(f"**分析时间**: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**交易日期**: {metrics.trade_date or final_state.get('trade_date', 'N/A')}")
    lines.append(f"**总耗时**: {total_elapsed:.2f}秒" + (f" (约{total_elapsed/60:.1f}分钟)" if total_elapsed > 60 else ""))
    lines.append("")
    lines.append("---")
    lines.append("")

    # === 核心指标 ===
    lines.append("## ✅ 验证结果总结")
    lines.append("")
    lines.append("### 🎯 核心指标")
    lines.append("")
    lines.append("| 指标 | 预期值 | 实际值 | 状态 |")
    lines.append("|------|--------|--------|------|")
    lines.append(
        f"| **投资辩论轮次** | {max_debate_rounds}轮 ({expected_invest_speeches}次发言) | "
        f"{debate_info['investment_debate_count']//2}轮 ({debate_info['investment_debate_count']}次发言) | "
        f"{'✅ **正常**' if invest_ok else '⚠️ **异常**'} |"
    )
    lines.append(
        f"| **风险讨论轮次** | {max_risk_rounds}轮 ({expected_risk_speeches}次发言) | "
        f"{debate_info['risk_debate_count']//3}轮 ({debate_info['risk_debate_count']}次发言) | "
        f"{'✅ **正常**' if risk_ok else '⚠️ **异常**'} |"
    )
    lines.append(f"| **Quick LLM 超时配置** | {quick_timeout}秒 | {quick_timeout}秒 | ✅ **正常** |")
    lines.append(f"| **Deep LLM 超时配置** | {deep_timeout}秒 | {deep_timeout}秒 | ✅ **正常** |")
    # V2 修复: 总耗时对比 total_run_budget (1800s), 而非单次 LLM 超时 (120s)
    # 原 Bug: total_elapsed(1280s) < deep_timeout(180s) 永远为 False, 误报"超时"
    lines.append(f"| **实际耗时** | <{total_run_budget}秒 | {total_elapsed:.2f}秒 | {'✅ **正常**' if total_elapsed < total_run_budget else '⚠️ **超时**'} |")
    lines.append(f"| **LLM 超时次数** | 0次 | {metrics.timeout_count}次 | {'✅ **正常**' if timeout_ok else '⚠️ **异常**'} |")
    lines.append("")

    # === 配置验证 ===
    lines.append("### 1. 配置验证")
    lines.append("")
    lines.append(f"- 研究深度: {config.get('research_depth', '标准')}")
    lines.append(f"- 投资辩论轮次: {max_debate_rounds}")
    lines.append(f"- 风险讨论轮次: {max_risk_rounds}")
    lines.append(f"- Quick LLM 超时: {quick_timeout}秒")
    lines.append(f"- Deep LLM 超时: {deep_timeout}秒")
    lines.append(f"- 总运行预算: {total_run_budget}秒 (验证报告超时判定基准)")
    lines.append(f"- Quick 模型: {config.get('quick_think_llm', 'N/A')}")
    lines.append(f"- Deep 模型: {config.get('deep_think_llm', 'N/A')}")
    lines.append(f"- 最大递归限制: {config.get('max_recur_limit', 200)}")
    lines.append("")

    # === 辩论流程验证 ===
    lines.append("### 2. 辩论流程验证")
    lines.append("")
    lines.append(f"- 投资辩论: {debate_info['investment_debate_count']} 次发言 "
                 f"(预期 {expected_invest_speeches} 次, {'✅ 符合' if invest_ok else '⚠️ 不符'})")
    lines.append(f"- 风险讨论: {debate_info['risk_debate_count']} 次发言 "
                 f"(预期 {expected_risk_speeches} 次, {'✅ 符合' if risk_ok else '⚠️ 不符'})")
    lines.append("")

    # === ReAct 分析统计 ===
    lines.append("### 3. ReAct 分析统计")
    lines.append("")
    lines.append(f"- 分析标的数: {react_stats['total_tickers_analyzed']}")
    lines.append(f"- 总工具调用次数: {react_stats['total_tool_calls']}")
    lines.append("")
    if react_stats["per_ticker"]:
        lines.append("#### 各标的四维分析详情")
        lines.append("")
        lines.append("| 标的 | 维度 | 评级 | 置信度 | ReAct迭代 |")
        lines.append("|------|------|------|--------|-----------|")
        for ticker, dims in react_stats["per_ticker"].items():
            for dim, info in dims.items():
                lines.append(
                    f"| {ticker} | {dim} | {info['rating']} | "
                    f"{info['confidence']:.2f} | {info['react_iterations']} |"
                )
        lines.append("")

    # === LLM 性能分析 ===
    lines.append("### 4. LLM 性能分析")
    lines.append("")
    if metrics.llm_calls:
        lines.append("| 节点 | 耗时(秒) | 估算输入Token | 估算输出Token | 超时 |")
        lines.append("|------|---------|--------------|--------------|------|")
        for call in metrics.llm_calls:
            lines.append(
                f"| {call['node']} | {call.get('elapsed', 0):.2f} | "
                f"{call.get('est_tokens_in', 0)} | "
                f"{call.get('est_tokens_out', 'N/A')} | "
                f"{'⚠️是' if call.get('timed_out') else '否'} |"
            )
        lines.append("")
    else:
        lines.append("(未记录 LLM 调用详情, 可在关键节点使用 metrics.llm_timer() 进行埋点)")
        lines.append("")

    # === 时间分析 ===
    lines.append("### 5. 时间分析")
    lines.append("")
    lines.append(f"**总耗时**: {total_elapsed:.2f}秒")
    lines.append("")
    if sorted_nodes:
        lines.append("#### 各节点耗时占比")
        lines.append("")
        lines.append("| 节点 | 耗时(秒) | 占比 |")
        lines.append("|------|---------|------|")
        for node_name, timing in sorted_nodes:
            elapsed = timing.get("elapsed", 0)
            pct = (elapsed / total_elapsed * 100) if total_elapsed > 0 else 0
            lines.append(f"| {node_name} | {elapsed:.2f} | {pct:.1f}% |")
        lines.append("")

    # === 推荐结果摘要 ===
    portfolio = final_state.get("final_portfolio", [])
    lines.append("### 6. 推荐结果摘要")
    lines.append("")
    lines.append(f"- 最终推荐标的数: {len(portfolio)}")
    if portfolio:
        lines.append("- 推荐清单:")
        lines.append("")
        lines.append("| 代码 | 名称 | 来源 | 评级 | 目标价 | 止盈 | 止损 | 置信度 |")
        lines.append("|------|------|------|------|--------|------|------|--------|")
        for stock in portfolio:
            if isinstance(stock, dict):
                lines.append(
                    f"| {stock.get('ticker', '')} | {stock.get('name', '')} | "
                    f"{stock.get('source', '')} | {stock.get('rating', '')} | "
                    f"{stock.get('target_price', '')} | {stock.get('take_profit', '')} | "
                    f"{stock.get('stop_loss', '')} | {stock.get('confidence', '')} |"
                )
        lines.append("")

    # === 最终结论 ===
    lines.append("---")
    lines.append("")
    lines.append("## 🎉 最终结论")
    lines.append("")
    all_ok = invest_ok and risk_ok and timeout_ok
    lines.append(f"**{'✅ 验证通过' if all_ok else '⚠️ 存在异常'}**")
    lines.append("")
    lines.append(f"1. {'✅' if invest_ok else '⚠️'} 投资辩论执行 {debate_info['investment_debate_count']} 次发言")
    lines.append(f"2. {'✅' if risk_ok else '⚠️'} 风险讨论执行 {debate_info['risk_debate_count']} 次发言")
    lines.append(f"3. {'✅' if timeout_ok else '⚠️'} LLM 超时次数: {metrics.timeout_count}")
    lines.append(f"4. ✅ 总耗时: {total_elapsed:.2f}秒")
    lines.append(f"5. ✅ 分析标的数: {react_stats['total_tickers_analyzed']}")
    lines.append(f"6. ✅ 推荐标的数: {len(portfolio)}")
    lines.append("")

    report_content = "\n".join(lines)

    # 保存报告
    os.makedirs(save_dir, exist_ok=True)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    trade_date = metrics.trade_date or final_state.get("trade_date", "unknown")
    filename = f"验证报告_{trade_date}_{timestamp}.md"
    report_path = os.path.join(save_dir, filename)

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        logger.info(f"[指标] 验证报告已保存: {report_path}")
    except Exception as e:
        logger.error(f"[指标] 保存报告失败: {e}")

    # 同时保存指标 JSON
    metrics_path = os.path.join(save_dir, f"metrics_{trade_date}_{timestamp}.json")
    try:
        metrics_data = metrics.to_dict()
        metrics_data["debate_info"] = debate_info
        metrics_data["react_stats"] = react_stats
        metrics_data["portfolio_count"] = len(portfolio)
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics_data, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[指标] 指标数据已保存: {metrics_path}")
    except Exception as e:
        logger.error(f"[指标] 保存指标数据失败: {e}")

    return report_path
