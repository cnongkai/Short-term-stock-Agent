"""
LLM 调用工具单元测试 (V2 新增)

验证 safe_llm_invoke 的超时 + 重试 + 兜底逻辑, 以及辅助函数:
  - _is_transient_error (瞬时错误判定)
  - FallbackResponse (兜底响应结构)
  - safe_llm_invoke (主入口: 正常/重试/超时/兜底/回调)

测试内容:
  1-4. _is_transient_error 各类错误判定
  5. FallbackResponse 属性
  6. 正常调用返回响应
  7. 瞬时错误重试成功
  8. 非瞬时错误不重试直接兜底
  9. 超时错误触发重试
  10. 重试耗尽返回兜底
  11. 无兜底抛异常
  12. 兜底时回调触发

运行: python -m pytest tests/test_llm_utils.py -v
"""
import os
import sys

# 确保项目根目录在 Python 路径中
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

import pytest

from stock_agent.agents.utils.llm_utils import (
    FallbackResponse,
    _is_transient_error,
    safe_llm_invoke,
)


# =====================================================================
# Mock LLM (可配置 invoke 行为: 成功/抛异常/前N次失败后成功)
# =====================================================================
class _MockResponse:
    """模拟 LLM 响应 (含 content 属性)"""
    def __init__(self, content="ok"):
        self.content = content


class _MockLLM:
    """可配置 invoke 行为的 Mock LLM

    Args:
        behavior: "success" (总是成功) / "raise" (总是抛 exc)
        exc: 抛出的异常实例
        fail_then_succeed: 前 N 次调用抛 exc, 之后成功
        response_content: 成功时返回的 content 文本
    """
    def __init__(self, behavior="success", exc=None, fail_then_succeed=0,
                 response_content="ok"):
        self.behavior = behavior
        self.exc = exc
        self.fail_then_succeed = fail_then_succeed
        self.response_content = response_content
        self.call_count = 0

    def invoke(self, prompt):
        self.call_count += 1
        if self.fail_then_succeed > 0 and self.call_count <= self.fail_then_succeed:
            raise self.exc
        if self.behavior == "raise":
            raise self.exc
        return _MockResponse(self.response_content)


# =====================================================================
# 测试 1: 瞬时错误 — 限流 (429)
# =====================================================================
def test_is_transient_error_rate_limit():
    """测试 429/rate_limit 错误判定为瞬时 (可重试)"""
    assert _is_transient_error(Exception("Error 429: rate_limit exceeded")) is True
    assert _is_transient_error(Exception("Rate limit hit")) is True
    assert _is_transient_error(Exception("rate limit exceeded")) is True


# =====================================================================
# 测试 2: 瞬时错误 — 服务器错误 (500/502/503/504)
# =====================================================================
def test_is_transient_error_server_error():
    """测试 500/502/503 服务器错误判定为瞬时"""
    assert _is_transient_error(Exception("500 Internal Server Error")) is True
    assert _is_transient_error(Exception("502 Bad Gateway")) is True
    assert _is_transient_error(Exception("503 Service Unavailable")) is True
    assert _is_transient_error(Exception("504 Gateway Timeout")) is True


# =====================================================================
# 测试 3: 瞬时错误 — 超时/连接断开
# =====================================================================
def test_is_transient_error_timeout_connection():
    """测试 TimeoutError/ConnectionError/RemoteDisconnected 判定为瞬时"""
    assert _is_transient_error(TimeoutError("operation timed out")) is True
    assert _is_transient_error(ConnectionError("connection reset by peer")) is True
    assert _is_transient_error(Exception("RemoteDisconnected: Remote end closed")) is True
    assert _is_transient_error(Exception("ReadTimeout: read timed out")) is True
    assert _is_transient_error(OSError("network unreachable")) is True


# =====================================================================
# 测试 4: 非瞬时错误 — 客户端错误 (400/401/403)
# =====================================================================
def test_is_transient_error_client_error():
    """测试 400/401/403 客户端错误判定为非瞬时 (不重试)"""
    assert _is_transient_error(ValueError("HTTP 400: invalid request")) is False
    assert _is_transient_error(Exception("HTTP 401: unauthorized")) is False
    assert _is_transient_error(Exception("HTTP 403: forbidden")) is False
    assert _is_transient_error(KeyError("missing_field")) is False


# =====================================================================
# 测试 5: FallbackResponse 属性
# =====================================================================
def test_fallback_response_attributes():
    """测试 FallbackResponse 含 .content/.tool_calls/.response_metadata/.additional_kwargs"""
    fb = FallbackResponse('{"candidates": []}')
    assert fb.content == '{"candidates": []}'
    assert fb.tool_calls is None
    assert fb.response_metadata == {"fallback": True}
    assert fb.additional_kwargs == {}


# =====================================================================
# 测试 6: 正常调用返回 LLM 响应
# =====================================================================
def test_safe_invoke_normal():
    """测试正常调用返回 LLM 响应, 不触发重试"""
    llm = _MockLLM(behavior="success", response_content='{"hot_topics": []}')
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=2,
        fallback_content='{"candidates": []}',
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert response.content == '{"hot_topics": []}'
    assert llm.call_count == 1, "正常调用应仅 invoke 1 次"


# =====================================================================
# 测试 7: 瞬时错误重试成功
# =====================================================================
def test_safe_invoke_transient_retry_success():
    """测试瞬时错误 (ConnectionError) 第1次失败, 第2次重试成功"""
    llm = _MockLLM(
        fail_then_succeed=1,
        exc=ConnectionError("connection reset"),
        response_content='{"hot_topics": ["AI"]}',
    )
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=2,
        fallback_content='{"candidates": []}',
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert response.content == '{"hot_topics": ["AI"]}'
    assert llm.call_count == 2, "瞬时错误应重试 1 次, 共 invoke 2 次"


# =====================================================================
# 测试 8: 非瞬时错误不重试, 直接兜底
# =====================================================================
def test_safe_invoke_non_transient_no_retry():
    """测试非瞬时错误 (400) 不重试, 直接返回兜底, 仅 invoke 1 次"""
    llm = _MockLLM(
        behavior="raise",
        exc=ValueError("HTTP 400: invalid request"),
    )
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=2,
        fallback_content='{"candidates": []}',
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert isinstance(response, FallbackResponse)
    assert response.content == '{"candidates": []}'
    assert llm.call_count == 1, "非瞬时错误不应重试, 仅 invoke 1 次"


# =====================================================================
# 测试 9: 超时错误触发重试
# =====================================================================
def test_safe_invoke_timeout_retry():
    """测试 TimeoutError 触发重试, 第2次成功"""
    llm = _MockLLM(
        fail_then_succeed=1,
        exc=TimeoutError("LLM 调用超时"),
        response_content='{"policy_events": []}',
    )
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=1,
        fallback_content='{"candidates": []}',
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert response.content == '{"policy_events": []}'
    assert llm.call_count == 2, "超时应重试 1 次, 共 invoke 2 次"


# =====================================================================
# 测试 10: 重试耗尽返回兜底
# =====================================================================
def test_safe_invoke_all_retries_exhausted_fallback():
    """测试持续瞬时错误, 重试耗尽后返回 FallbackResponse"""
    llm = _MockLLM(
        behavior="raise",
        exc=ConnectionError("connection reset"),
    )
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=1,
        fallback_content='{"decisions": []}',
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert isinstance(response, FallbackResponse)
    assert response.content == '{"decisions": []}'
    assert llm.call_count == 2, "max_retries=1 应 invoke 2 次 (1 初始 + 1 重试)"


# =====================================================================
# 测试 11: 无兜底抛异常
# =====================================================================
def test_safe_invoke_no_fallback_raises():
    """测试 fallback_content=None 时, 重试耗尽抛出最后异常"""
    llm = _MockLLM(
        behavior="raise",
        exc=ConnectionError("connection reset"),
    )
    with pytest.raises(ConnectionError):
        safe_llm_invoke(
            llm, "test prompt",
            timeout=90, max_retries=1,
            fallback_content=None,  # 无兜底
            retry_base_wait=0.0, retry_max_wait=0.0,
            node_name="测试",
        )
    assert llm.call_count == 2, "max_retries=1 应 invoke 2 次后抛异常"


# =====================================================================
# 测试 12: 兜底时 on_fallback 回调触发
# =====================================================================
def test_safe_invoke_on_fallback_callback():
    """测试兜底时 on_fallback 回调被调用一次"""
    callback_count = [0]

    def _on_fallback():
        callback_count[0] += 1

    llm = _MockLLM(
        behavior="raise",
        exc=ConnectionError("connection reset"),
    )
    response = safe_llm_invoke(
        llm, "test prompt",
        timeout=90, max_retries=0,  # 不重试, 直接兜底
        fallback_content='{"decisions": []}',
        on_fallback=_on_fallback,
        retry_base_wait=0.0, retry_max_wait=0.0,
        node_name="测试",
    )
    assert isinstance(response, FallbackResponse)
    assert callback_count[0] == 1, "on_fallback 回调应被调用 1 次"
    assert llm.call_count == 1, "max_retries=0 应仅 invoke 1 次"
