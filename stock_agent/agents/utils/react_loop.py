"""
可复用 ReAct 迭代循环 (对应 PRD 6.7, V2 核心创新)

ReAct = Reasoning + Acting + Observation 的迭代循环:
  1. 思考(Reasoning): LLM 分析已有数据, 识别信息缺口
  2. 行动(Acting): LLM 调用工具补充数据
  3. 观察(Observation): 分析工具返回结果
  4. 循环直至信息充分或达到迭代上限

约束 (PRD 6.7):
  - 最大迭代次数: react_max_iterations (默认 5)
  - 每次迭代最大工具调用数: react_max_tools_per_iter (默认 3)
  - 单个分析师总工具调用上限: react_max_total_tools (默认 10)

参考架构: TradingAgents-CN agents/analysts/fundamentals_analyst.py 的 ReAct 循环模式,
抽象为可复用组件, 供 4 个分析师 (基本面/技术/A股专属/个股发展) 共享调用。

关键修复 (日志排查):
  - OpenAI/DeepSeek 400 错误 "tool_calls must be followed by tool messages":
    根因是 LLM 返回的 tool_call 有时 id 为空字符串, 旧逻辑 `if tc_id and ...`
    会跳过空 id 的占位 ToolMessage, 导致该 tool_call 无响应 → 400。
    修复: 为缺失 id 生成合成 id, 并保证每个 tool_call 都有对应 ToolMessage。
  - 工具返回 list-of-lists 导致下游 `.items()` 报错:
    在工具层统一归一化为 list-of-dicts (见 dataflows/providers 与 tools 防御)。
"""
import json
import time
import uuid
from typing import Any, List, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger

# V2: 委托给共享工具, 避免代码重复 (原 _invoke_with_timeout 已抽出到 llm_utils.py)
from stock_agent.agents.utils.llm_utils import _invoke_with_timeout


def run_react_loop(
    llm,
    tools: list,
    system_prompt: str,
    user_query: str,
    config: dict = None,
    analyst_name: str = None,
) -> Tuple[str, List[dict]]:
    """执行 ReAct 迭代循环

    Args:
        llm: LLM 实例 (已绑定工具或可绑定)
        tools: 可用工具列表 (langchain Tool 实例)
        system_prompt: 系统提示词 (定义分析师角色与输出格式)
        user_query: 用户查询 (含股票代码/名称/分析目标)
        config: 配置 (含 react_max_iterations 等约束)
        analyst_name: 分析师名称 (V2 优化, 用于读取 per-analyst ReAct 覆盖配置)
            如 "stock_development"/"fundamentals"/"technical"/"china_specific",
            对应 config["react_per_analyst"][analyst_name] 中的覆盖项。

    Returns:
        (final_analysis, tool_log):
          - final_analysis: LLM 最终分析文本 (应为 JSON 格式)
          - tool_log: 工具调用日志列表 [{name, args, result_summary, timestamp, iteration, elapsed, success}]
    """
    config = config or {}
    # V2 优化: 按分析师维度覆盖 ReAct 约束 (stock_development 等高迭代维度降配)
    # 优先级: react_per_analyst[name] > 全局 react_max_* > 默认值
    per_analyst = config.get("react_per_analyst", {})
    overrides = per_analyst.get(analyst_name, {}) if analyst_name else {}
    max_iterations = overrides.get("max_iterations", config.get("react_max_iterations", 5))
    max_tools_per_iter = overrides.get("max_tools_per_iter", config.get("react_max_tools_per_iter", 3))
    max_total_tools = overrides.get("max_total_tools", config.get("react_max_total_tools", 10))
    # LLM 硬超时 (V2: 防挂起, 默认 90s, 小于 ChatOpenAI 的 120s 作为最硬兜底)
    llm_call_timeout = config.get("llm_call_hard_timeout", 90)
    if analyst_name:
        logger.info(
            f"[ReAct] 分析师={analyst_name}, 约束: 迭代上限={max_iterations}/"
            f"每轮工具={max_tools_per_iter}/总工具={max_total_tools}"
        )

    tool_log: List[dict] = []
    total_tool_calls = 0

    # === 构建 LLM (绑定工具) ===
    if tools:
        llm_with_tools = llm.bind_tools(tools)
    else:
        llm_with_tools = llm
        logger.warning("[ReAct] 无可用工具, LLM 将仅基于已有信息推理")

    # 构建工具查找表 (name → tool 实例, 用于执行)
    tool_map = {}
    for tool in tools:
        name = getattr(tool, "name", getattr(tool, "__name__", str(tool)))
        tool_map[name] = tool

    # === 初始化消息 ===
    messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_query),
    ]

    # === ReAct 迭代循环 (PRD 6.7) ===
    for iteration in range(1, max_iterations + 1):
        iter_start = time.time()
        logger.debug(f"[ReAct] 迭代 {iteration}/{max_iterations}, 已调用工具 {total_tool_calls}/{max_total_tools}")

        # --- 思考(Reasoning): 调用 LLM (V2: 硬超时防挂起) ---
        try:
            response = _invoke_with_timeout(llm_with_tools, messages, llm_call_timeout)
        except TimeoutError as e:
            # LLM 硬超时: 直接返回兜底 JSON, 不走 400 恢复 (恢复也要调 LLM, 同样会超时)
            logger.error(f"[ReAct] LLM 调用硬超时 (迭代 {iteration}, {llm_call_timeout}s): {e}")
            fallback_json = json.dumps(
                {"rating": "中性", "confidence": 0.1, "report": f"LLM调用超时({llm_call_timeout}s)"},
                ensure_ascii=False,
            )
            return fallback_json, tool_log
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[ReAct] LLM 调用失败 (迭代 {iteration}): {err_msg}")
            # 400 错误恢复: 消息历史中可能存在未配对 tool_calls 的 AIMessage
            # 策略: 移除末尾含 tool_calls 的 AIMessage, 用无工具 LLM 生成最终分析
            if "tool_calls" in err_msg or "400" in err_msg:
                logger.warning("[ReAct] 检测到 tool_calls 配对错误, 尝试恢复 (剥离含 tool_calls 的 AIMessage)")
                recovered = _recover_from_tool_call_error(messages, llm, llm_call_timeout)
                if recovered:
                    return recovered, tool_log
            break

        messages.append(response)

        # --- 检查是否有工具调用 ---
        tool_calls = getattr(response, "tool_calls", None)

        if not tool_calls:
            # 无工具调用 → 分析完成, 返回最终文本
            iter_elapsed = time.time() - iter_start
            logger.info(f"[ReAct] 迭代 {iteration} 完成 (耗时 {iter_elapsed:.2f}s), LLM 未请求工具调用, 分析结束")
            return response.content, tool_log

        # --- 行动(Acting): 执行工具调用 ---
        # 关键: 为每个 tool_call 保证一个 ToolMessage 响应 (避免 400 错误)
        tools_this_iter = 0
        responded_ids = set()  # 已生成 ToolMessage 的 tool_call_id

        for tc in tool_calls:
            # 提取/补全 tool_call_id (DeepSeek 偶尔返回空 id, 导致配对失败)
            tool_call_id = tc.get("id") or ""
            if not tool_call_id:
                tool_call_id = f"call_{uuid.uuid4().hex[:8]}"
                # 回写到 tc, 便于日志一致性
                tc["id"] = tool_call_id

            tool_name = tc.get("name", "unknown")
            tool_args = tc.get("args", {}) or {}

            # 检查约束: 是否允许执行该工具
            reached_total = total_tool_calls >= max_total_tools
            reached_iter = tools_this_iter >= max_tools_per_iter

            if reached_total or reached_iter:
                # 不执行, 但必须补一个占位 ToolMessage (否则 400)
                if reached_total:
                    reason = f"达到总工具调用上限 ({max_total_tools})"
                else:
                    reason = f"达到本次迭代工具上限 ({max_tools_per_iter})"
                logger.warning(f"[ReAct] 跳过工具 {tool_name}: {reason}")
                messages.append(ToolMessage(
                    content=f"工具调用被跳过 ({reason})。请基于已有数据继续分析并输出最终结论。",
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))
                responded_ids.add(tool_call_id)
                continue

            # 执行工具
            logger.info(f"[ReAct] 调用工具: {tool_name} | 参数: {tool_args}")
            tool_start = time.time()
            tool_result, success = _execute_tool(tool_name, tool_args, tool_map)
            tool_elapsed = time.time() - tool_start

            # 记录日志 (PRD 8.7: 工具调用日志写入分析报告)
            log_entry = {
                "name": tool_name,
                "args": tool_args,
                "result_summary": _truncate_result(tool_result),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "iteration": iteration,
                "elapsed": round(tool_elapsed, 2),
                "success": success,
            }
            tool_log.append(log_entry)
            total_tool_calls += 1
            tools_this_iter += 1
            responded_ids.add(tool_call_id)

            # --- 观察(Observation): 将工具结果加入消息 ---
            messages.append(ToolMessage(
                content=str(tool_result),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))

        # 安全校验: 确保本轮所有 tool_call 都有 ToolMessage 响应 (防止 400)
        _ensure_all_tool_calls_responded(tool_calls, messages, responded_ids)

        iter_elapsed = time.time() - iter_start
        logger.debug(f"[ReAct] 迭代 {iteration} 完成 (耗时 {iter_elapsed:.2f}s), 本轮工具调用 {tools_this_iter} 次")

        # 检查是否达到总工具上限 → 再调一次 LLM (不带工具) 生成最终分析
        if total_tool_calls >= max_total_tools:
            logger.warning(f"[ReAct] 达到总工具调用上限, 强制结束循环")
            try:
                final_response = _invoke_with_timeout(llm, messages, llm_call_timeout)
                return final_response.content, tool_log
            except TimeoutError:
                logger.error(f"[ReAct] 最终 LLM 调用硬超时({llm_call_timeout}s), 返回兜底")
                fallback_json = json.dumps(
                    {"rating": "中性", "confidence": 0.1, "report": f"LLM调用超时({llm_call_timeout}s)"},
                    ensure_ascii=False,
                )
                return fallback_json, tool_log
            except Exception as e:
                logger.error(f"[ReAct] 最终 LLM 调用失败: {e}")
                # 兜底: 返回最后一次 response
                return response.content, tool_log

    # === 达到迭代上限, 返回最后一次 LLM 输出 ===
    logger.warning(f"[ReAct] 达到最大迭代次数 ({max_iterations}), 返回最后一次输出")
    # 末尾若为含 tool_calls 的 AIMessage, 用无工具 LLM 生成最终结论 (避免返回工具调用而非分析)
    if messages and isinstance(messages[-1], AIMessage) and getattr(messages[-1], "tool_calls", None):
        try:
            final_response = _invoke_with_timeout(llm, messages, llm_call_timeout)
            return final_response.content, tool_log
        except TimeoutError:
            logger.error(f"[ReAct] 迭代上限后 LLM 调用硬超时({llm_call_timeout}s), 返回兜底")
            fallback_json = json.dumps(
                {"rating": "中性", "confidence": 0.1, "report": f"LLM调用超时({llm_call_timeout}s)"},
                ensure_ascii=False,
            )
            return fallback_json, tool_log
        except Exception as e:
            logger.error(f"[ReAct] 迭代上限后最终 LLM 调用失败: {e}")
    if messages and isinstance(messages[-1], AIMessage):
        return messages[-1].content, tool_log

    # 兜底: 返回空分析
    return "分析未完成: 达到迭代上限且无有效输出", tool_log


def _ensure_all_tool_calls_responded(tool_calls: list, messages: list, responded_ids: set) -> None:
    """确保本轮所有 tool_call 都有对应 ToolMessage (修复 400 错误的核心)。

    旧逻辑仅在 tc_id 非空时补占位消息, 导致空 id 的 tool_call 无响应 → 400。
    新逻辑: 对未响应的 tool_call 一律补占位 ToolMessage (id 缺失则合成)。
    """
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        if not tc_id:
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            tc["id"] = tc_id
        if tc_id not in responded_ids:
            logger.warning(f"[ReAct] 补占位 ToolMessage (此前漏响应): {tc.get('name', 'unknown')} id={tc_id}")
            messages.append(ToolMessage(
                content="工具调用被跳过。请基于已有数据继续分析并输出最终结论。",
                tool_call_id=tc_id,
                name=tc.get("name", "unknown"),
            ))
            responded_ids.add(tc_id)


def _recover_from_tool_call_error(messages: list, llm, timeout: int = 90) -> str:
    """从 tool_calls 配对错误中恢复: 剥离末尾含 tool_calls 的 AIMessage, 用无工具 LLM 生成最终分析。"""
    try:
        # 从后向前移除含 tool_calls 的 AIMessage 及其后的 ToolMessage
        while messages and isinstance(messages[-1], AIMessage) and getattr(messages[-1], "tool_calls", None):
            messages.pop()
        # 移除末尾可能残留的 ToolMessage (已无对应 AIMessage)
        while messages and isinstance(messages[-1], ToolMessage):
            messages.pop()
        if not messages:
            return ""
        # 追加一条 HumanMessage 引导 LLM 输出最终结论
        messages.append(HumanMessage(content="请基于以上已获取的数据, 直接输出最终分析结论 (严格 JSON 格式), 不要再调用工具。"))
        final_response = _invoke_with_timeout(llm, messages, timeout)
        return getattr(final_response, "content", str(final_response))
    except TimeoutError:
        logger.error(f"[ReAct] 恢复时 LLM 调用超时({timeout}s)")
        return json.dumps(
            {"rating": "中性", "confidence": 0.1, "report": f"LLM调用超时({timeout}s)"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"[ReAct] 恢复失败: {e}")
        return ""


def _execute_tool(tool_name: str, tool_args: dict, tool_map: dict) -> Tuple[str, bool]:
    """执行单个工具调用 (容错: 失败返回错误描述, 不抛异常)

    Args:
        tool_name: 工具名称
        tool_args: 工具参数
        tool_map: 工具名 → 实例 的映射

    Returns:
        (工具执行结果字符串, 是否成功)
    """
    tool = tool_map.get(tool_name)
    if tool is None:
        return f"错误: 未知工具 {tool_name}", False

    try:
        # langchain Tool 实例可通过 .invoke() 或直接调用
        if hasattr(tool, "invoke"):
            result = tool.invoke(tool_args)
        else:
            result = tool(**tool_args)
        return str(result), True
    except Exception as e:
        logger.error(f"[ReAct] 工具 {tool_name} 执行失败: {e}")
        return f"工具执行错误: {tool_name} - {e}", False


def _truncate_result(result: str, max_len: int = 500) -> str:
    """截断工具结果摘要 (避免日志过长, PRD 8.7)"""
    if not result:
        return ""
    if len(result) <= max_len:
        return result
    return result[:max_len] + "...(已截断)"
