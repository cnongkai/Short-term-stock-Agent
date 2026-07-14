"""
工具调用日志装饰器 (对应 PRD 8.7 工具调用日志全链路留痕)

提供 @log_analyst_module 和 @log_tool_call 装饰器,
记录分析师模块入口和工具调用, 确保 PRD 要求的"全链路可追溯"。
"""
import functools
import time
import json
from typing import Callable

from loguru import logger


def log_analyst_module(analyst_name: str) -> Callable:
    """分析师模块入口日志装饰器

    Args:
        analyst_name: 分析师名称 (如 "fundamentals")
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(state):
            ticker = state.get("trade_date", "unknown")
            logger.info(f"{'=' * 60}")
            logger.info(f"[{analyst_name}] 分析师节点开始 | 交易日期: {ticker}")
            logger.info(f"{'=' * 60}")
            start_time = time.time()
            try:
                result = func(state)
                elapsed = time.time() - start_time
                logger.info(f"[{analyst_name}] 分析师节点完成 | 耗时: {elapsed:.2f}s")
                return result
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[{analyst_name}] 分析师节点异常 | 耗时: {elapsed:.2f}s | 错误: {e}")
                raise

        return wrapper

    return decorator


def log_tool_call(tool_name: str) -> Callable:
    """工具调用日志装饰器 (PRD 8.7: 工具调用日志写入分析报告)"""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            logger.info(f"[工具调用] {tool_name} | 参数: {kwargs or args}")
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time
                # 记录返回摘要 (截断避免日志过长)
                try:
                    summary = json.dumps(result, ensure_ascii=False, default=str)
                    summary = summary[:500] + ("..." if len(summary) > 500 else "")
                except Exception:
                    summary = str(result)[:500]
                logger.info(f"[工具返回] {tool_name} | 耗时: {elapsed:.2f}s | 摘要: {summary}")
                return result
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[工具失败] {tool_name} | 耗时: {elapsed:.2f}s | 错误: {e}")
                raise

        return wrapper

    return decorator
