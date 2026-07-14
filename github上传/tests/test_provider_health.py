"""
数据源熔断器单元测试 (V2 新增)

验证 ProviderHealthTracker 的三态熔断逻辑:
  CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN

测试内容:
  1. 单例模式
  2. 初始状态 (CLOSED, 不跳过)
  3. 阈值未达不熔断
  4. 达阈值触发熔断 (OPEN)
  5. record_success 重置状态
  6. 冷却到期 → HALF_OPEN
  7. HALF_OPEN 成功 → CLOSED
  8. HALF_OPEN 失败 → OPEN (冷却加倍)
  9. reset 清空所有记录
  10. get_stats 返回正确结构
  11. 不同 provider+method 互不影响
  12. total_failures 累计不归零

运行: python -m pytest tests/test_provider_health.py -v
"""
import os
import sys
import time

# 确保项目根目录在 Python 路径中
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from stock_agent.dataflows.provider_health import (
    ProviderHealthTracker,
    get_health_tracker,
    CLOSED,
    OPEN,
    HALF_OPEN,
)


def _fresh_tracker(threshold: int = 3, cooldown: int = 120) -> ProviderHealthTracker:
    """获取重置后的单例, 并设置指定阈值/冷却 (测试辅助)"""
    tracker = get_health_tracker()
    tracker.reset()
    tracker._failure_threshold = threshold
    tracker._cooldown = cooldown
    return tracker


# =====================================================================
# 测试 1: 单例模式
# =====================================================================
def test_singleton():
    """测试 get_health_tracker 多次调用返回同一实例"""
    t1 = get_health_tracker()
    t2 = get_health_tracker()
    assert t1 is t2, "get_health_tracker 应返回同一单例"
    assert isinstance(t1, ProviderHealthTracker)


# =====================================================================
# 测试 2: 初始状态不跳过
# =====================================================================
def test_initial_state_no_skip():
    """测试新 key 初始为 CLOSED, should_skip 返回 False"""
    tracker = _fresh_tracker()
    assert tracker.should_skip("NewProvider", "get_stock_info") is False


# =====================================================================
# 测试 3: 未达阈值不熔断
# =====================================================================
def test_below_threshold_no_open():
    """测试阈值=3 时, 2 次失败后仍 CLOSED (should_skip=False)"""
    tracker = _fresh_tracker(threshold=3)
    tracker.record_failure("ProvA", "get_stock_info")
    tracker.record_failure("ProvA", "get_stock_info")
    assert tracker.should_skip("ProvA", "get_stock_info") is False
    # 验证状态仍为 CLOSED
    rec = tracker._records["ProvA.get_stock_info"]
    assert rec.state == CLOSED
    assert rec.failure_count == 2


# =====================================================================
# 测试 4: 达阈值触发熔断
# =====================================================================
def test_threshold_triggers_open():
    """测试第 3 次失败触发 OPEN, should_skip 返回 True"""
    tracker = _fresh_tracker(threshold=3)
    for _ in range(3):
        tracker.record_failure("ProvB", "get_stock_info")
    assert tracker.should_skip("ProvB", "get_stock_info") is True
    rec = tracker._records["ProvB.get_stock_info"]
    assert rec.state == OPEN
    assert rec.open_until > 0


# =====================================================================
# 测试 5: record_success 重置状态
# =====================================================================
def test_record_success_resets():
    """测试 record_success 重置 failure_count=0, state=CLOSED"""
    tracker = _fresh_tracker(threshold=3)
    # 先制造 2 次失败 (未熔断)
    tracker.record_failure("ProvC", "get_stock_info")
    tracker.record_failure("ProvC", "get_stock_info")
    rec = tracker._records["ProvC.get_stock_info"]
    assert rec.failure_count == 2

    tracker.record_success("ProvC", "get_stock_info")
    assert rec.failure_count == 0
    assert rec.state == CLOSED
    assert rec.open_until == 0.0

    # 在 OPEN 状态下 record_success 也应恢复
    tracker.record_failure("ProvD", "get_stock_info")
    tracker.record_failure("ProvD", "get_stock_info")
    tracker.record_failure("ProvD", "get_stock_info")
    rec_d = tracker._records["ProvD.get_stock_info"]
    assert rec_d.state == OPEN
    tracker.record_success("ProvD", "get_stock_info")
    assert rec_d.state == CLOSED
    assert rec_d.failure_count == 0


# =====================================================================
# 测试 6: 冷却到期 → HALF_OPEN
# =====================================================================
def test_cooldown_expiry_to_half_open():
    """测试 OPEN 冷却到期后进入 HALF_OPEN (should_skip 返回 False)"""
    tracker = _fresh_tracker(threshold=3, cooldown=120)
    for _ in range(3):
        tracker.record_failure("ProvE", "get_stock_info")
    rec = tracker._records["ProvE.get_stock_info"]
    assert rec.state == OPEN

    # 模拟冷却到期: 将 open_until 设为过去时间
    rec.open_until = time.time() - 1

    # should_skip 应返回 False 并转为 HALF_OPEN
    assert tracker.should_skip("ProvE", "get_stock_info") is False
    assert rec.state == HALF_OPEN


# =====================================================================
# 测试 7: HALF_OPEN 成功 → CLOSED
# =====================================================================
def test_half_open_success_closes():
    """测试 HALF_OPEN 状态 record_success → CLOSED"""
    tracker = _fresh_tracker(threshold=3, cooldown=120)
    for _ in range(3):
        tracker.record_failure("ProvF", "get_stock_info")
    rec = tracker._records["ProvF.get_stock_info"]
    # 进入 HALF_OPEN
    rec.open_until = time.time() - 1
    tracker.should_skip("ProvF", "get_stock_info")
    assert rec.state == HALF_OPEN

    # 试探成功 → CLOSED
    tracker.record_success("ProvF", "get_stock_info")
    assert rec.state == CLOSED
    assert rec.failure_count == 0
    assert rec.open_until == 0.0


# =====================================================================
# 测试 8: HALF_OPEN 失败 → OPEN (渐进退避: 首次 2x)
# =====================================================================
def test_half_open_failure_reopens_doubled():
    """测试 HALF_OPEN 试探失败 → OPEN, 首次冷却加倍 (2x = 240s)"""
    tracker = _fresh_tracker(threshold=3, cooldown=120)
    for _ in range(3):
        tracker.record_failure("ProvG", "get_stock_info")
    rec = tracker._records["ProvG.get_stock_info"]
    base_open_until = rec.open_until

    # 进入 HALF_OPEN
    rec.open_until = time.time() - 1
    tracker.should_skip("ProvG", "get_stock_info")
    assert rec.state == HALF_OPEN

    # 试探失败 → OPEN, 首次渐进退避 2x (240s)
    tracker.record_failure("ProvG", "get_stock_info")
    assert rec.state == OPEN
    assert rec.probe_failures == 1
    # 冷却加倍: open_until 应 = now + cooldown*2 (240s)
    now = time.time()
    assert rec.open_until > now + 200, f"冷却应加倍至 ~240s, 实际 open_until={rec.open_until}, now={now}"
    # 验证 should_skip 再次返回 True
    assert tracker.should_skip("ProvG", "get_stock_info") is True


# =====================================================================
# 测试 8b: HALF_OPEN 渐进退避 (多次探测失败, 冷却上限 4x)
# =====================================================================
def test_half_open_progressive_backoff():
    """测试 HALF_OPEN 多次探测失败的渐进退避: 2x → 4x → 4x (上限)"""
    tracker = _fresh_tracker(threshold=3, cooldown=120)
    for _ in range(3):
        tracker.record_failure("ProvG2", "get_stock_info")
    rec = tracker._records["ProvG2.get_stock_info"]

    # 第 1 次探测失败 → 2x (240s)
    rec.open_until = time.time() - 1
    tracker.should_skip("ProvG2", "get_stock_info")  # → HALF_OPEN
    tracker.record_failure("ProvG2", "get_stock_info")  # → OPEN
    assert rec.probe_failures == 1
    now1 = time.time()
    cooldown1 = rec.open_until - now1
    assert 230 <= cooldown1 <= 250, f"第1次退避应 ~240s (2x), 实际 {cooldown1:.0f}s"

    # 第 2 次探测失败 → 4x (480s)
    rec.open_until = time.time() - 1
    tracker.should_skip("ProvG2", "get_stock_info")  # → HALF_OPEN
    tracker.record_failure("ProvG2", "get_stock_info")  # → OPEN
    assert rec.probe_failures == 2
    now2 = time.time()
    cooldown2 = rec.open_until - now2
    assert 470 <= cooldown2 <= 490, f"第2次退避应 ~480s (4x), 实际 {cooldown2:.0f}s"

    # 第 3 次探测失败 → 仍 4x (上限 480s, 不再翻倍)
    rec.open_until = time.time() - 1
    tracker.should_skip("ProvG2", "get_stock_info")  # → HALF_OPEN
    tracker.record_failure("ProvG2", "get_stock_info")  # → OPEN
    assert rec.probe_failures == 3
    now3 = time.time()
    cooldown3 = rec.open_until - now3
    assert 470 <= cooldown3 <= 490, f"第3次退避应 ~480s (上限4x), 实际 {cooldown3:.0f}s"

    # record_success 应重置 probe_failures
    tracker.record_success("ProvG2", "get_stock_info")
    assert rec.probe_failures == 0
    assert rec.state == CLOSED


# =====================================================================
# 测试 9: reset 清空所有记录
# =====================================================================
def test_reset_clears_all():
    """测试 reset() 后所有记录清空, get_stats 返回空 dict"""
    tracker = _fresh_tracker(threshold=3)
    tracker.record_failure("ProvH", "get_stock_info")
    tracker.record_failure("ProvI", "get_news")
    assert len(tracker._records) > 0

    tracker.reset()
    assert len(tracker._records) == 0
    assert tracker.get_stats() == {}
    # reset 后 should_skip 对任意 key 返回 False
    assert tracker.should_skip("ProvH", "get_stock_info") is False


# =====================================================================
# 测试 10: get_stats 返回正确结构
# =====================================================================
def test_get_stats_format():
    """测试 get_stats 返回正确字段结构"""
    tracker = _fresh_tracker(threshold=2)
    tracker.record_failure("ProvJ", "get_stock_info")
    tracker.record_failure("ProvJ", "get_stock_info")  # 达阈值 → OPEN

    stats = tracker.get_stats()
    assert "ProvJ.get_stock_info" in stats
    entry = stats["ProvJ.get_stock_info"]
    assert "state" in entry
    assert "failure_count" in entry
    assert "total_failures" in entry
    assert "open_until" in entry
    assert entry["state"] == OPEN
    assert entry["failure_count"] == 2
    assert entry["total_failures"] == 2
    assert entry["open_until"] > 0


# =====================================================================
# 测试 11: 不同 provider+method 互不影响
# =====================================================================
def test_independent_keys():
    """测试不同 provider+method 的熔断状态相互独立"""
    tracker = _fresh_tracker(threshold=3)
    # 让 AkShare.get_stock_info 熔断
    for _ in range(3):
        tracker.record_failure("AkShareProvider", "get_stock_info")
    assert tracker.should_skip("AkShareProvider", "get_stock_info") is True

    # BaoStock.get_stock_info 不受影响
    assert tracker.should_skip("BaoStockProvider", "get_stock_info") is False
    # AkShare.get_news 不受影响
    assert tracker.should_skip("AkShareProvider", "get_news") is False

    # 验证 BaoStock.get_stock_info 仍可正常累计失败
    tracker.record_failure("BaoStockProvider", "get_stock_info")
    rec = tracker._records["BaoStockProvider.get_stock_info"]
    assert rec.state == CLOSED
    assert rec.failure_count == 1


# =====================================================================
# 测试 12: total_failures 累计不归零
# =====================================================================
def test_total_failures_accumulates():
    """测试 total_failures 累计, record_success 不重置 total_failures"""
    tracker = _fresh_tracker(threshold=3)
    # 3 次失败 → total_failures=3
    for _ in range(3):
        tracker.record_failure("ProvK", "get_stock_info")
    rec = tracker._records["ProvK.get_stock_info"]
    assert rec.total_failures == 3

    # 成功 → failure_count 归零, total_failures 保持
    tracker.record_success("ProvK", "get_stock_info")
    assert rec.failure_count == 0
    assert rec.total_failures == 3

    # 再失败 1 次 → failure_count=1, total_failures=4
    tracker.record_failure("ProvK", "get_stock_info")
    assert rec.failure_count == 1
    assert rec.total_failures == 4

    # get_stats 仍包含此记录 (total_failures > 0)
    stats = tracker.get_stats()
    assert "ProvK.get_stock_info" in stats
    assert stats["ProvK.get_stock_info"]["total_failures"] == 4
