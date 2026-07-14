"""
政策信源模块 (对应 PRD 6.3, V2 新增 6 类权威政策信源)

  - manager : PolicySourceManager (6 信源抓取 + 合规降级)

6 类信源: 证监会 / 央行 / 发改委 / 交易所 / 巨潮 / 四大证券报+新华社
合规: robots.txt 遵守 + 限速 (≤1次/5分钟) + 摘要提取 + URL 标注
"""
from stock_agent.dataflows.providers.policy.manager import PolicySourceManager

__all__ = ["PolicySourceManager"]
