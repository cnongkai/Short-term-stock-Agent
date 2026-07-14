"""
LLM 输出 JSON 解析助手

LLM 输出的 JSON 可能被 markdown 代码块包裹或包含额外文本,
本模块提供容错解析, 支持:
  1. 直接 json.loads
  2. 提取 ```json ... ``` 代码块
  3. 提取首个 { ... } 或 [ ... ] 块
  4. 全部失败时返回默认值
"""
import re
import json
from typing import Any, Optional


def safe_json_parse(text: str, default: Any = None) -> Any:
    """从 LLM 输出文本中安全解析 JSON

    Args:
        text: LLM 输出文本 (可能含 markdown 代码块或额外说明)
        default: 解析失败时的返回值

    Returns:
        解析后的 Python 对象 (dict/list), 或 default
    """
    if not text:
        return default

    # 尝试 1: 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass

    # 尝试 2: 提取 ```json ... ``` 或 ``` ... ``` 代码块
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 尝试 3: 提取首个 { ... } 块 (对象)
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    # 尝试 4: 提取首个 [ ... ] 块 (数组)
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return default


def truncate_text(text: str, max_len: int = 2000) -> str:
    """截断文本到指定长度 (避免 prompt 过长)"""
    if not text or len(text) <= max_len:
        return text or ""
    return text[:max_len] + "...(已截断)"


def safe_json_parse_dict(text: str, default: dict = None) -> dict:
    """安全解析 JSON 并确保返回 dict。

    修复问题: LLM 偶尔输出 JSON 数组 [...] 而非对象 {...},
    导致后续 result["key"] = value 报错 "list indices must be integers or slices, not str"。
    本函数在 safe_json_parse 基础上增加类型校验, 非 dict 时返回 default。

    Args:
        text: LLM 输出文本
        default: 解析失败或结果非 dict 时的返回值

    Returns:
        保证返回 dict (要么是解析出的对象, 要么是 default)
    """
    if default is None:
        default = {}
    result = safe_json_parse(text, default=default)
    if not isinstance(result, dict):
        # LLM 输出了 JSON 数组或其它非对象类型, 使用默认值
        return dict(default)
    return result
