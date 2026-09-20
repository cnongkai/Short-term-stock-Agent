"""
短线股票推荐 Agent — 入口脚本 (对应 PRD Phase 0 / M9)

使用方法:
  1. 复制 .env.example 为 .env, 填入 DEEPSEEK_API_KEY
  2. 运行: python main.py --date 2026-07-09

流程:
  load_config() 加载配置 → StockRecommendationGraph 初始化 →
  propagate(trade_date) 执行全流程 → 输出推荐报告

参考架构: TradingAgents-CN 主入口
"""
import argparse
import atexit
import sys
import os
from datetime import datetime

# 确保项目根目录在 Python 路径中 (使 stock_agent 包可导入)
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from loguru import logger

from config.settings import load_config
from stock_agent.graph.trading_graph import StockRecommendationGraph


def setup_logging(debug: bool = False):
    """配置日志 (PRD: 全链路留痕)"""
    logger.remove()
    level = "DEBUG" if debug else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
    )
    logger.add(
        os.path.join(_PROJECT_DIR, "results", "logs", "agent_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="1 day",
        retention="7 days",
        encoding="utf-8",
    )


def main():
    """主入口: 解析参数 → 加载配置 → 执行推荐流程"""
    parser = argparse.ArgumentParser(description="短线股票推荐 Agent (PRD V2)")
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="交易日期 (YYYY-MM-DD), 默认今天",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="调试模式 (输出 DEBUG 级别日志)",
    )
    parser.add_argument(
        "--risk",
        type=str,
        default="moderate",
        choices=["conservative", "moderate", "aggressive"],
        help="风险偏好 (PRD M9 用户配置)",
    )
    parser.add_argument(
        "--holding",
        type=str,
        default="1-5",
        help="持仓周期(交易日), 如 1-5 / 5-10",
    )
    args = parser.parse_args()

    # === 配置日志 ===
    setup_logging(debug=args.debug)

    logger.info("=" * 60)
    logger.info("短线股票推荐 Agent 启动 (PRD V2)")
    logger.info(f"交易日期: {args.date}")
    logger.info(f"风险偏好: {args.risk} | 持仓周期: {args.holding}")
    logger.info("=" * 60)

    # === 加载配置 (PRD Phase 0: .env 中的 API Key) ===
    config = load_config()
    logger.info(f"[配置] LLM: {config['llm_provider']} / {config['quick_think_llm']}")

    # === 用户配置 (PRD M9) ===
    user_config = {
        "risk_preference": args.risk,
        "holding_period": args.holding,
        "sector_preference": [],  # 空表示不限制板块
        "max_position": 1,  # 单只最大仓位 30%
    }

    # === 初始化图并执行 (PRD 全流程) ===
    try:
        graph = StockRecommendationGraph(config=config, debug=args.debug)
    except ValueError as e:
        logger.error(f"[初始化失败] {e}")
        logger.error("请在 .env 文件中配置正确的 API Key (参考 .env.example)")
        sys.exit(1)

    # === 执行推荐 ===
    final_state, decision = graph.propagate(
        trade_date=args.date,
        user_config=user_config,
    )

    # === 输出摘要 ===
    portfolio = final_state.get("final_portfolio", [])
    logger.info("=" * 60)
    logger.info(f"[推荐完成] 共推荐 {len(portfolio)} 只标的")
    logger.info(f"[决策摘要] {decision[:200] if decision else '无'}")
    logger.info("=" * 60)

    # 打印推荐清单
    if portfolio:
        print("\n" + "=" * 60)
        print(f"  短线推荐清单 ({args.date})")
        print("=" * 60)
        for i, stock in enumerate(portfolio, 1):
            ticker = stock.get("ticker", "?")
            name = stock.get("name", "?")
            source = stock.get("source", "?")
            rating = stock.get("rating", "?")
            target = stock.get("target_price", "?")
            stop = stock.get("stop_loss", "?")
            conf = stock.get("confidence", "?")
            print(f"  {i}. {ticker} {name} [{source}]")
            print(f"     评级: {rating} | 目标价: ¥{target} | 止损: ¥{stop} | 置信度: {conf}")
        print("=" * 60)
    else:
        print("\n[提示] 本周期无推荐标的 (候选池为空或触发熔断)")

    logger.info(f"[结果] 报告已保存至 {config['results_dir']}/")

    # === 清理资源 (V3: 修复进程挂起问题) ===
    _cleanup_resources()


def _cleanup_resources():
    """清理后台资源: AkShare线程池 / BaoStock登出 / 缓存关闭

    问题根因: AkShareProvider 创建了 ThreadPoolExecutor(max_workers=2) 但从未 shutdown,
    这些非 daemon 线程阻止 Python 进程退出。BaoStock 登录后也未 logout。
    """
    from stock_agent.dataflows.data_source_manager import get_manager

    try:
        manager = get_manager()
        for provider in manager._providers:
            name = provider.__class__.__name__
            # AkShare: 关闭超时线程池
            if hasattr(provider, '_timeout_executor'):
                try:
                    provider._timeout_executor.shutdown(wait=False)
                    logger.debug(f"[清理] {name} 线程池已关闭")
                except Exception:
                    pass
            # BaoStock: 登出
            if hasattr(provider, 'logout') and hasattr(provider, '_logged_in'):
                try:
                    provider.logout()
                    logger.debug(f"[清理] {name} 已登出")
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"[清理] 资源清理异常 (不影响结果): {e}")

    # 关闭缓存
    try:
        from stock_agent.dataflows.cache import get_cache_store
        get_cache_store().shutdown()
    except Exception:
        pass


# 注册 atexit 钩子 (确保异常退出时也清理)
atexit.register(_cleanup_resources)


if __name__ == "__main__":
    main()
    # 强制退出: AkShare/BaoStock 的非 daemon 线程会阻止正常退出,
    # 清理后用 os._exit(0) 确保进程立即终止 (V3 修复进程挂起)
    import os as _os
    _os._exit(0)
