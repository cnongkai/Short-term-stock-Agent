"""
日志工具: 阶段计时与关键产物打印 (用于排查执行耗时和逻辑问题)

提供 log_stage 上下文管理器, 在关键节点内部对各阶段进行细粒度计时,
输出 INFO 级别日志 (默认可见), 便于线上排查耗时瓶颈和逻辑问题。

用法:
    from stock_agent.agents.utils.log_utils import log_stage

    with log_stage("获取板块数据", "板块推理"):
        sector_list = _fetch_sector_data_raw()
    # 输出: [板块推理][获取板块数据] 开始
    #       [板块推理][获取板块数据] 完成, 耗时 0.85s

    # 也可记录阶段产物:
    with log_stage("五维赋分", "板块推理") as stage:
        scored = [_score_sector(s, attention_map) for s in sector_list]
        stage.result = f"{len(scored)} 个板块已赋分"
"""
import time
from contextlib import contextmanager
from loguru import logger


class _StageContext:
    """阶段上下文 (允许在 with 块内设置 result 摘要)"""
    def __init__(self):
        self.result: str = ""


@contextmanager
def log_stage(stage_name: str, node_name: str = ""):
    """阶段计时上下文管理器 (INFO 级别)

    在节点内部对各执行阶段进行细粒度计时, 输出开始/完成日志。
    与 RunMetrics.node_timer 互补: node_timer 用于节点级计时 (写入指标),
    log_stage 用于阶段级计时 (仅日志, 不写入指标, 避免并行竞态)。

    Args:
        stage_name: 阶段名称 (如 "获取板块数据")
        node_name: 所属节点名称 (如 "板块推理"), 可选

    Yields:
        _StageContext: 可设置 .result 属性记录阶段产物摘要
    """
    prefix = f"[{node_name}]" if node_name else ""
    ctx = _StageContext()
    start = time.perf_counter()
    logger.info(f"{prefix}[{stage_name}] 开始")
    try:
        yield ctx
    finally:
        elapsed = time.perf_counter() - start
        result_suffix = f" | {ctx.result}" if ctx.result else ""
        logger.info(f"{prefix}[{stage_name}] 完成, 耗时 {elapsed:.2f}s{result_suffix}")
