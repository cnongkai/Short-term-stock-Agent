"""
分析层图节点 (对应 PRD Phase 4 / 6.4 端到端流程)

遍历候选池, 对每只股票执行四维 ReAct 分析 (基本面/技术/A股专属/个股发展),
结果聚合存入 analysis_reports[ticker]。

图节点接口: create_analysis_layer(llm, toolkit, config) → analysis_layer_node(state) -> dict

PRD 8.5 分析报告结构:
  analysis_reports = {
    "ticker": {
      "fundamentals":     {rating, confidence, key_metrics, ...},
      "technical":        {rating, confidence, key_metrics, ...},
      "china_specific":   {rating, confidence, key_metrics, ...},
      "stock_development":{rating, confidence, key_events, ...}
    }
  }

V2 优化: 四维 ReAct 分析改为 ThreadPoolExecutor 并行执行 (默认 4 维并行),
标的层默认串行 (analysis_parallel_stocks=1), 可配置为 2 提升吞吐。
线程安全: metrics.timeout_count 已加 Lock, node_timings/llm_calls 在 GIL 下原子。

参考架构: TradingAgents-CN 的分析师节点 (本项目扩展为遍历候选池 + 并行)
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger

from stock_agent.agents.analysis.fundamentals_analyst import analyze_fundamentals
from stock_agent.agents.analysis.technical_analyst import analyze_technical
from stock_agent.agents.analysis.china_specific_analyst import analyze_china_specific
from stock_agent.agents.analysis.stock_development_analyst import analyze_stock_development
from stock_agent.graph.run_metrics import get_metrics


def create_analysis_layer(llm, toolkit, config: dict):
    """创建分析层图节点

    Args:
        llm: LLM 实例 (quick_thinking_llm)
        toolkit: Toolkit 工具包
        config: 系统配置 (含 analysis_parallel_dimensions / analysis_max_workers 等)

    Returns:
        analysis_layer_node(state) -> dict: 更新 analysis_reports + react_tool_call_counts
    """

    def analysis_layer_node(state) -> dict:
        logger.info(f"[分析层] 开始四维 ReAct 分析")

        metrics = get_metrics()

        # === 读取状态 ===
        candidate_pool = state.get("candidate_pool", [])
        trade_date = state.get("trade_date", "")
        existing_reports = state.get("analysis_reports", {})

        if not candidate_pool:
            logger.warning("[分析层] 候选池为空, 跳过分析")
            return {"analysis_reports": {}}

        # === 批量预取候选池数据 (V2: 分析层前预热缓存, 工具调用全部命中缓存) ===
        if config.get("prefetch_enabled", True) and candidate_pool:
            try:
                from stock_agent.dataflows.interface import get_manager
                with metrics.node_timer("prefetch"):
                    pf = get_manager().prefetch_ticker_data(candidate_pool, trade_date)
                    logger.info(
                        f"[分析层] 预取完成: {pf['prefetched']}只, "
                        f"失败{len(pf['failed'])}项, 耗时{pf['elapsed']:.1f}s"
                    )
            except Exception as e:
                logger.warning(f"[分析层] 预取失败(不影响后续, 工具会按需拉取): {e}")

        analysis_reports = dict(existing_reports)
        react_counts = dict(state.get("react_tool_call_counts", {}))

        # === 并行配置 (V2 优化) ===
        parallel_dims = config.get("analysis_parallel_dimensions", True)
        max_workers = config.get("analysis_max_workers", 4)
        parallel_stocks = config.get("analysis_parallel_stocks", 1)
        # 超时配置 (V2: 防挂起, 单维超时/标的超时, 超时填兜底评级继续推进)
        dim_timeout = config.get("analysis_dimension_timeout", 120)
        stock_timeout = config.get("analysis_stock_timeout", 300)

        logger.info(
            f"[分析层] 并行配置: 维度并行={parallel_dims}, max_workers={max_workers}, 标的并行={parallel_stocks}"
        )

        # === 四维分析规格 (维度名, 分析函数, 异常兜底评级) ===
        dim_specs = [
            ("fundamentals", analyze_fundamentals, "中性"),
            ("technical", analyze_technical, "中性"),
            ("china_specific", analyze_china_specific, "中性"),
            ("stock_development", analyze_stock_development, "无催化"),
        ]

        def analyze_dimension(dim_name, analyze_fn, fallback_rating):
            """构建单维分析闭包 (可并行执行, 带节点计时与异常兜底)"""
            def _wrap(ticker, name):
                with metrics.node_timer(f"{dim_name}_{ticker}"):
                    try:
                        return dim_name, analyze_fn(llm, toolkit, ticker, name, trade_date, config)
                    except Exception as e:
                        logger.error(f"[分析层] {ticker} {dim_name} 分析异常: {e}")
                        return dim_name, {
                            "rating": fallback_rating,
                            "confidence": 0.1,
                            "report": f"分析异常: {e}",
                        }
            return _wrap

        def process_stock(stock):
            """处理单只标的: 执行四维分析 (可并行), 返回 (ticker, report, total_tools)"""
            ticker = stock.get("ticker", "")
            name = stock.get("name", "")
            source = stock.get("source", "")
            logger.info(f"[分析层] 分析 {ticker} {name} [{source}]")

            ticker_report = {}

            if parallel_dims and max_workers > 1:
                # === 四维并行 (V2: ThreadPoolExecutor + 超时防挂起) ===
                # 关键: 不用 with 语句 (with 的 __exit__ 会阻塞等待所有线程),
                # 改为手动管理 + as_completed(timeout) + fut.result(timeout),
                # 超时后 cancel + shutdown(wait=False) 立即释放, 填兜底评级继续推进。
                worker_count = min(max_workers, len(dim_specs))
                ex = ThreadPoolExecutor(max_workers=worker_count)
                futures = {
                    ex.submit(analyze_dimension(dim, fn, fb), ticker, name): dim
                    for dim, fn, fb in dim_specs
                }
                try:
                    for fut in as_completed(futures, timeout=stock_timeout):
                        dim_name = futures[fut]
                        try:
                            _, result = fut.result(timeout=dim_timeout)
                            ticker_report[dim_name] = result
                        except TimeoutError:
                            fut.cancel()
                            logger.error(f"[分析层] {ticker} {dim_name} 单维超时({dim_timeout}s)")
                            ticker_report[dim_name] = {
                                "rating": "中性", "confidence": 0.1,
                                "report": f"单维分析超时({dim_timeout}s)",
                            }
                        except Exception as e:
                            logger.error(f"[分析层] {ticker} {dim_name} future 异常: {e}")
                            ticker_report[dim_name] = {
                                "rating": "中性", "confidence": 0.1, "report": f"并行异常: {e}",
                            }
                except TimeoutError:
                    # 标的级超时: 未完成的维度填兜底
                    logger.error(f"[分析层] {ticker} 标的级超时({stock_timeout}s), 未完成维度填兜底")
                    for fut, dim_name in futures.items():
                        if not fut.done():
                            fut.cancel()
                            ticker_report.setdefault(dim_name, {
                                "rating": "中性", "confidence": 0.1,
                                "report": f"标的超时({stock_timeout}s)",
                            })
                finally:
                    ex.shutdown(wait=False)  # 关键: 不阻塞等待挂起线程
            else:
                # === 串行回退 (兼容旧配置) ===
                for dim, fn, fb in dim_specs:
                    _, ticker_report[dim] = analyze_dimension(dim, fn, fb)(ticker, name)

            # 统计该标的总工具调用数 (PRD 8.7: 全链路留痕)
            total_tools = sum(
                ticker_report.get(d, {}).get("react_iterations", 0)
                for d, _, _ in dim_specs
            )
            logger.info(f"[分析层] {ticker} 四维分析完成, 总工具调用: {total_tools} 次")
            return ticker, ticker_report, total_tools

        # === 标的层并行/串行 ===
        if parallel_stocks > 1:
            # 多标的并行 (可选, 默认关闭以规避 LLM 限流)
            # V2: 不用 with 语句, 手动管理 + 超时防挂起
            stock_total_timeout = stock_timeout * len(candidate_pool)
            ex = ThreadPoolExecutor(max_workers=parallel_stocks)
            futures = {ex.submit(process_stock, stock): stock for stock in candidate_pool}
            try:
                for fut in as_completed(futures, timeout=stock_total_timeout):
                    try:
                        ticker, ticker_report, total_tools = fut.result(timeout=stock_timeout)
                        analysis_reports[ticker] = ticker_report
                        react_counts[ticker] = total_tools
                    except TimeoutError:
                        stock = futures[fut]
                        logger.error(f"[分析层] 标的 {stock.get('ticker','?')} 超时({stock_timeout}s)")
                        analysis_reports[stock.get("ticker", "?")] = {}
                    except Exception as e:
                        stock = futures[fut]
                        logger.error(f"[分析层] 标的 {stock.get('ticker','?')} 处理异常: {e}")
            except TimeoutError:
                logger.error(f"[分析层] 标的层总超时({stock_total_timeout}s)")
                for fut, stock in futures.items():
                    if not fut.done():
                        fut.cancel()
                        analysis_reports.setdefault(stock.get("ticker", "?"), {})
            finally:
                ex.shutdown(wait=False)
        else:
            # 标的串行, 每只标的内部四维并行 (默认模式)
            for idx, stock in enumerate(candidate_pool, 1):
                logger.info(f"[分析层] ({idx}/{len(candidate_pool)}) 处理标的")
                ticker, ticker_report, total_tools = process_stock(stock)
                analysis_reports[ticker] = ticker_report
                react_counts[ticker] = total_tools

        logger.info(f"[分析层] 全部 {len(candidate_pool)} 只标的四维分析完成")

        return {
            "analysis_reports": analysis_reports,
            "react_tool_call_counts": react_counts,
        }

    return analysis_layer_node
