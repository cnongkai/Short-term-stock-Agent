"""
数据源 Provider 熔断器 (V2 新增)

按 provider+method 维度跟踪连续失败次数, 达阈值后熔断 (跳过该 provider+method),
冷却到期后进入 HALF_OPEN 允许一次试探, 探测成功则恢复, 失败则重新熔断。

设计模式: Circuit Breaker (三态 CLOSED → OPEN → HALF_OPEN → CLOSED)
线程安全: 使用 threading.Lock 保护所有状态读写 (prefetch 与分析层并行调用同一 manager)

使用方式:
    from stock_agent.dataflows.provider_health import get_health_tracker

    tracker = get_health_tracker()
    if tracker.should_skip("AkShareProvider", "get_stock_info"):
        continue  # 熔断中, 跳过
    try:
        result = provider.get_stock_info(ticker)
        if result and result.get("data"):
            tracker.record_success("AkShareProvider", "get_stock_info")
        else:
            tracker.record_failure("AkShareProvider", "get_stock_info")
    except Exception:
        tracker.record_failure("AkShareProvider", "get_stock_info")
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from loguru import logger


# 熔断状态常量
CLOSED = "closed"          # 正常调用
OPEN = "open"              # 熔断中, 跳过
HALF_OPEN = "half_open"    # 探测期, 允许一次试探


@dataclass
class HealthRecord:
    """单个 provider+method 的健康记录"""
    failure_count: int = 0           # 连续失败次数
    last_failure_ts: float = 0.0     # 最近失败时间戳
    open_until: float = 0.0          # 熔断到期时间戳 (0 = 未熔断)
    state: str = CLOSED
    total_failures: int = 0          # 累计失败总数 (用于统计)
    probe_failures: int = 0          # HALF_OPEN 探测失败次数 (渐进退避)


class ProviderHealthTracker:
    """provider+method 级熔断器 (单例, 线程安全)

    熔断逻辑:
      1. CLOSED 状态: 正常调用, 每次失败 failure_count++, 达阈值 → OPEN
      2. OPEN 状态: should_skip() 返回 True, 直到冷却到期 → HALF_OPEN
      3. HALF_OPEN 状态: should_skip() 返回 False (允许一次试探)
         - 试探成功 → CLOSED (重置计数)
         - 试探失败 → OPEN (冷却加倍)
    """

    _instance: Optional["ProviderHealthTracker"] = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 120):
        if self._initialized:
            return
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._records: Dict[str, HealthRecord] = {}
        self._lock = threading.Lock()
        self._initialized = True
        logger.info(
            f"[熔断器] 初始化: 阈值={failure_threshold}次, 冷却={cooldown_seconds}s"
        )

    def _key(self, provider_name: str, method_name: str) -> str:
        """生成 provider+method 维度的键"""
        return f"{provider_name}.{method_name}"

    def should_skip(self, provider_name: str, method_name: str) -> bool:
        """是否应跳过该 provider+method (熔断中)

        Returns:
            True = 熔断中应跳过, False = 可以尝试调用
        """
        key = self._key(provider_name, method_name)
        with self._lock:
            rec = self._records.get(key)
            if rec is None or rec.state == CLOSED:
                return False
            now = time.time()
            if rec.state == OPEN:
                if now >= rec.open_until:
                    # 冷却到期, 进入 HALF_OPEN: 允许一次试探
                    rec.state = HALF_OPEN
                    logger.debug(f"[熔断器] {key} 冷却到期 → HALF_OPEN (试探)")
                    return False
                return True  # 仍在冷却期, 跳过
            # HALF_OPEN: 不跳过 (试探中)
            return False

    def record_success(self, provider_name: str, method_name: str):
        """调用成功 → 重置计数, 关闭熔断"""
        key = self._key(provider_name, method_name)
        with self._lock:
            rec = self._records.get(key)
            if rec is not None:
                if rec.state != CLOSED:
                    logger.info(f"[熔断器] {key} 恢复正常 → CLOSED")
                rec.failure_count = 0
                rec.state = CLOSED
                rec.open_until = 0.0
                rec.probe_failures = 0  # 重置探测失败计数

    def record_failure(self, provider_name: str, method_name: str):
        """调用失败 → 累计失败, 达阈值则熔断

        HALF_OPEN 探测失败时使用渐进退避:
          第 1 次探测失败: cooldown * 2  (240s, 原 V2 行为)
          第 2 次探测失败: cooldown * 4  (480s)
          第 3+ 次探测失败: cooldown * 4 (上限 480s, 避免过长黑名单)
        避免对持续不可用的 provider 反复试探浪费时间。
        """
        key = self._key(provider_name, method_name)
        with self._lock:
            rec = self._records.setdefault(key, HealthRecord())
            now = time.time()
            rec.failure_count += 1
            rec.total_failures += 1
            rec.last_failure_ts = now

            if rec.state == HALF_OPEN:
                # 探测失败 → 重新熔断, 渐进退避
                rec.probe_failures += 1
                # 退避倍数: 2, 4, 4, 4... (上限 4x, 防止过激黑名单)
                backoff = min(2 ** rec.probe_failures, 4)
                cooldown = self._cooldown * backoff
                rec.state = OPEN
                rec.open_until = now + cooldown
                logger.warning(
                    f"[熔断器] {key} HALF_OPEN 探测失败 (第{rec.probe_failures}次) → OPEN "
                    f"(冷却 {cooldown}s, 退避倍数={backoff}x)"
                )
            elif rec.failure_count >= self._failure_threshold:
                rec.state = OPEN
                rec.open_until = now + self._cooldown
                logger.warning(
                    f"[熔断器] {key} 连续失败 {rec.failure_count} 次 → OPEN "
                    f"(冷却 {self._cooldown}s)"
                )

    def get_stats(self) -> dict:
        """获取熔断器统计 (用于验证报告)"""
        with self._lock:
            return {
                key: {
                    "state": rec.state,
                    "failure_count": rec.failure_count,
                    "total_failures": rec.total_failures,
                    "open_until": rec.open_until,
                    "probe_failures": rec.probe_failures,
                }
                for key, rec in self._records.items()
                if rec.total_failures > 0 or rec.state != CLOSED
            }

    def reset(self):
        """重置所有记录 (测试用)"""
        with self._lock:
            self._records.clear()


def get_health_tracker(
    failure_threshold: int = 3,
    cooldown_seconds: int = 120,
) -> ProviderHealthTracker:
    """获取全局 ProviderHealthTracker 单例

    首次调用时用传入参数初始化, 后续调用忽略参数 (单例)。
    """
    return ProviderHealthTracker(
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )
