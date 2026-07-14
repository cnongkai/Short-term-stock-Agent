"""
LLM 调用共享工具: 超时 + 重试 + 兜底 (V2 新增)

为所有图节点提供统一的 LLM 调用保护层:
- 硬超时 (ThreadPoolExecutor + future.result, 兼容子线程)
- 瞬时错误重试 (429/500/502/503/TimeoutError/ConnectionError, 指数退避, 最多 2 次)
- 最终失败返回兜底响应 (带 .content 属性, 避免下游 AttributeError)

设计参考: react_loop.py 的 _invoke_with_timeout (抽出为通用)

使用方式:
    from stock_agent.agents.utils.llm_utils import safe_llm_invoke

    # 发现层/决策层节点
    response = safe_llm_invoke(
        llm, prompt,
        timeout=90,
        max_retries=2,
        fallback_content='{"candidates": []}',
        node_name="主线选股",
    )
    result = safe_json_parse(response.content, default={"candidates": []})

    # 需要记录超时次数时
    response = safe_llm_invoke(
        llm, prompt,
        ...,
        on_fallback=metrics.record_timeout,
    )
"""
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional, Union

from loguru import logger


# === 瞬时错误判定 (可安全重试的错误) ===
_TRANSIENT_MARKERS = (
    "429", "rate_limit", "Rate limit", "rate limit",
    "500", "502", "503", "504",
    "Internal Server Error", "Bad Gateway", "Service Unavailable",
    "Connection", "Timeout", "timed out",
    "RemoteDisconnected", "RemoteProtocolError",
    "ReadTimeout", "ConnectError", "ConnectionError",
    "APITimeoutError", "TimeoutError",
)


def _is_transient_error(err: Exception) -> bool:
    """判断是否为可重试的瞬时错误

    瞬时错误 (可重试): 429 限流, 500/502/503 服务器错误, 超时, 连接断开
    非瞬时错误 (不重试): 400 参数错误, 401 认证失败, 403 权限不足
    """
    err_str = str(err)
    err_type = type(err).__name__
    # Python 内置超时/连接异常
    if isinstance(err, (TimeoutError, ConnectionError, OSError)):
        return True
    for marker in _TRANSIENT_MARKERS:
        if marker in err_str or marker in err_type:
            return True
    return False


class FallbackResponse:
    """兜底响应对象 (模拟 LangChain AIMessage 的 .content 属性)

    当 LLM 调用全部失败时返回此对象, 使调用方的 response.content 访问不报错。
    """

    def __init__(self, content: str):
        self.content = content
        # 模拟 tool_calls 属性 (react_loop 可能访问)
        self.tool_calls = None
        self.response_metadata = {"fallback": True}
        # 模拟 additional_kwargs (某些 LangChain 版本会访问)
        self.additional_kwargs = {}

    def __repr__(self) -> str:
        return f"FallbackResponse(content={self.content[:50]!r}...)"


def _invoke_with_timeout(llm, prompt_or_messages, timeout: int):
    """硬超时包裹 LLM 调用 (ThreadPoolExecutor 模式, 兼容子线程)

    signal.alarm 仅主线程可用, 而分析层四维并行是在子线程,
    因此用 ThreadPoolExecutor(1) + future.result(timeout) 实现:
    - 超时后 cancel + shutdown(wait=False), 不阻塞当前线程
    - 挂起的工作线程最终会因 httpx/socket 超时自行退出 (I/O 阻塞型)

    Args:
        llm: LLM 实例 (有 invoke 方法)
        prompt_or_messages: prompt 字符串 或 LangChain Message 列表
        timeout: 超时秒数 (<=0 表示不超时)

    Returns:
        LLM 响应

    Raises:
        TimeoutError: 超时未返回
    """
    if not timeout or timeout <= 0:
        return llm.invoke(prompt_or_messages)
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(llm.invoke, prompt_or_messages)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeoutError:
            fut.cancel()
            raise TimeoutError(f"LLM 调用超时 ({timeout}s)")
        finally:
            ex.shutdown(wait=False)


def safe_llm_invoke(
    llm,
    prompt_or_messages: Union[str, list],
    timeout: int = 90,
    max_retries: int = 2,
    retry_base_wait: float = 1.0,
    retry_max_wait: float = 3.0,
    fallback_content: Optional[str] = None,
    node_name: str = "",
    on_fallback: Optional[Callable[[], None]] = None,
) -> Any:
    """安全 LLM 调用: 超时 + 重试 + 兜底

    Args:
        llm: LangChain LLM 实例 (有 invoke 方法)
        prompt_or_messages: prompt 字符串 或 LangChain Message 列表
        timeout: 硬超时秒数 (默认 90, 小于 ChatOpenAI 的 120s)
        max_retries: 瞬时错误最大重试次数 (默认 2)
        retry_base_wait: 重试基础等待秒数 (指数退避 base: 1s, 2s, 4s...)
        retry_max_wait: 重试最大等待秒数 (退避上限)
        fallback_content: 最终失败时返回的兜底文本 (None 则抛异常)
        node_name: 节点名 (仅用于日志标识)
        on_fallback: 兜底时回调 (如 metrics.record_timeout)

    Returns:
        LLM 响应对象 (有 .content 属性), 或 FallbackResponse

    Raises:
        最后一次重试仍失败时, 若 fallback_content 为 None 则抛出最后异常
    """
    tag = f"[{node_name}]" if node_name else "[LLM]"
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            response = _invoke_with_timeout(llm, prompt_or_messages, timeout)
            if attempt > 0:
                logger.info(f"{tag} 第 {attempt} 次重试成功")
            return response
        except TimeoutError as e:
            last_exc = e
            if attempt < max_retries:
                logger.warning(
                    f"{tag} 调用超时 ({timeout}s), 重试 {attempt + 1}/{max_retries}"
                )
                wait = min(retry_base_wait * (2 ** attempt), retry_max_wait)
                time.sleep(wait)
                continue
            logger.error(f"{tag} 调用超时 ({timeout}s), 重试已耗尽")
        except Exception as e:
            last_exc = e
            transient = _is_transient_error(e)
            err_brief = str(e)[:120]
            if transient and attempt < max_retries:
                logger.warning(
                    f"{tag} 调用失败 ({type(e).__name__}: {err_brief}), "
                    f"瞬时错误, 重试 {attempt + 1}/{max_retries}"
                )
                wait = min(retry_base_wait * (2 ** attempt), retry_max_wait)
                time.sleep(wait)
                continue
            # 非瞬时错误或重试已耗尽
            logger.error(
                f"{tag} 调用失败 ({type(e).__name__}: {err_brief}), "
                f"瞬时={transient}, 不重试"
            )
            break

    # 全部失败 → 返回兜底或抛异常
    if fallback_content is not None:
        logger.warning(f"{tag} 返回兜底响应: {fallback_content[:60]}")
        if on_fallback:
            try:
                on_fallback()
            except Exception as cb_err:
                logger.debug(f"{tag} on_fallback 回调异常: {cb_err}")
        return FallbackResponse(fallback_content)
    raise last_exc
