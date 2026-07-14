"""
板块推理器 V2 单元测试

验证:
  1. 五维赋分 (_score_sector) 计算正确
  2. 拥挤度地图 (_build_crowding_map) 颜色梯度映射正确
  3. 环比计算 (_calc_mom) 边界情况
  4. 向后兼容输出 (_to_ranked_sector) 字段完整
  5. 节点集成 (create_sector_inference) 返回正确结构
  6. 工具函数 (_safe_float / _normalize_positive) 边界
"""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from stock_agent.agents.discovery import sector_inference as si


# =====================================================================
# 合成历史板块数据 (替代已删除的 _get_static_sectors, 供历史兜底测试)
# 键名对齐 _load_prev_scores 返回格式 (英文键), 覆盖正/负流入与涨跌
# =====================================================================
_FAKE_HISTORICAL = {
    "半导体": {"change_pct": 3.0, "fund_flow": 20e8, "turnover": 4.0,
              "up_count": 90, "down_count": 10, "components": ["600584", "300750"],
              "total_score": 60.0},
    "医药": {"change_pct": -2.0, "fund_flow": -5e8, "turnover": 2.0,
            "up_count": 20, "down_count": 80, "components": [], "total_score": 20.0},
    "新能源": {"change_pct": 2.5, "fund_flow": 15e8, "turnover": 5.0,
              "up_count": 75, "down_count": 25, "components": ["300274"],
              "total_score": 55.0},
    "银行": {"change_pct": 0.3, "fund_flow": 2e8, "turnover": 1.0,
            "up_count": 50, "down_count": 50, "components": [], "total_score": 30.0},
    "房地产": {"change_pct": -1.5, "fund_flow": -3e8, "turnover": 1.5,
              "up_count": 30, "down_count": 70, "components": [], "total_score": 18.0},
    "消费": {"change_pct": 1.8, "fund_flow": 8e8, "turnover": 3.0,
            "up_count": 65, "down_count": 35, "components": [], "total_score": 42.0},
    "军工": {"change_pct": 2.2, "fund_flow": 10e8, "turnover": 4.5,
            "up_count": 70, "down_count": 30, "components": [], "total_score": 48.0},
    "化工": {"change_pct": -1.0, "fund_flow": -2e8, "turnover": 2.5,
            "up_count": 35, "down_count": 65, "components": [], "total_score": 22.0},
}


# =====================================================================
# 测试 1: 五维赋分
# =====================================================================
class TestScoreSector:
    def test_basic_scoring(self):
        """测试基本五维赋分 (正流入板块)"""
        sector = {
            "板块名称": "半导体",
            "涨跌幅": 3.0,
            "主力净流入": 20e8,
            "换手率": 4.0,
            "上涨家数": 90,
            "下跌家数": 10,
            "领涨股": ["600584", "300750"],
        }
        attention = {"半导体": 40}

        result = si._score_sector(sector, attention)

        assert result is not None
        assert result["sector"] == "半导体"
        # 五维都在 0-100 范围
        for dim in ["heat", "diffusion", "volatility", "rebound", "crowding"]:
            assert 0 <= result[dim] <= 100, f"{dim}={result[dim]} 超出 0-100"
        # 正流入 + 高上涨占比 → 回补力应较高
        assert result["rebound"] > 30
        # 成分股保留
        assert result["components"] == ["600584", "300750"]

    def test_negative_fund_flow(self):
        """测试资金流出板块 (热度/拥挤度应较低)"""
        sector = {
            "板块名称": "医药",
            "涨跌幅": -2.0,
            "主力净流入": -5e8,
            "换手率": 2.0,
            "上涨家数": 20,
            "下跌家数": 80,
        }
        result = si._score_sector(sector, {})
        # 负流入 → 归一化为 0, 热度仅来自注意力(0)
        assert result["heat"] == 0.0
        assert result["crowding"] == 0.0
        # 负涨幅 → 正涨幅贡献为 0, 但上涨占比(20/100=0.2)仍贡献 0.2*40=8.0
        # 回补力 = max(0, change_pct)*10 + up_ratio*40 = 0 + 0.2*40 = 8.0
        assert result["rebound"] == 8.0

    def test_missing_name_returns_none(self):
        """测试缺少板块名返回 None"""
        result = si._score_sector({"涨跌幅": 1.0}, {})
        assert result is None

    def test_all_metrics_in_range(self):
        """测试历史兜底全部板块五维都在 0-100"""
        with patch.object(si, "_load_prev_scores", return_value=_FAKE_HISTORICAL):
            sectors = si._get_historical_sectors("2026-07-11")
        assert len(sectors) == 8  # _FAKE_HISTORICAL 8 个板块
        for s in sectors:
            result = si._score_sector(s, {})
            assert result is not None
            for dim in ["heat", "diffusion", "volatility", "rebound", "crowding"]:
                assert 0 <= result[dim] <= 100


# =====================================================================
# 测试 2: 拥挤度地图颜色梯度
# =====================================================================
class TestCrowdingMap:
    def _make_scored(self, crowding, total_score=50, sector="测试"):
        return {
            "sector": sector,
            "crowding": crowding,
            "total_score": total_score,
            "mom_change": "+1.0%",
            "heat": 50, "diffusion": 50, "volatility": 50, "rebound": 50,
        }

    def test_red_gradient(self):
        """拥挤度 >= 80 → 🔴 极度拥挤"""
        m = si._build_crowding_map([self._make_scored(85, sector="高温")], "2026-07-11")
        assert m["top_10"][0]["color"] == "red"
        assert m["top_10"][0]["icon"] == "🔴"
        assert m["top_10"][0]["label"] == "极度拥挤"

    def test_orange_gradient(self):
        """拥挤度 60-79 → 🟠 拥挤"""
        m = si._build_crowding_map([self._make_scored(65, sector="温热")], "2026-07-11")
        assert m["top_10"][0]["color"] == "orange"
        assert m["top_10"][0]["icon"] == "🟠"

    def test_yellow_gradient(self):
        """拥挤度 40-59 → 🟡 适中"""
        m = si._build_crowding_map([self._make_scored(50, sector="适中")], "2026-07-11")
        assert m["top_10"][0]["color"] == "yellow"
        assert m["top_10"][0]["icon"] == "🟡"

    def test_green_gradient(self):
        """拥挤度 20-39 → 🟢 宽松"""
        m = si._build_crowding_map([self._make_scored(25, sector="宽松")], "2026-07-11")
        assert m["top_10"][0]["color"] == "green"
        assert m["top_10"][0]["icon"] == "🟢"

    def test_blue_gradient(self):
        """拥挤度 < 20 → 🔵 极度宽松"""
        m = si._build_crowding_map([self._make_scored(10, sector="冰冷")], "2026-07-11")
        assert m["top_10"][0]["color"] == "blue"
        assert m["top_10"][0]["icon"] == "🔵"

    def test_map_structure(self):
        """测试地图结构完整 (top_10 / map_text / trade_date)"""
        m = si._build_crowding_map([self._make_scored(70, sector="测试")], "2026-07-11")
        assert "top_10" in m
        assert "map_text" in m
        assert m["trade_date"] == "2026-07-11"
        assert "测试" in m["map_text"]
        # 每个条目含 rank/sector/crowding/total_score/color/icon/label/metrics
        item = m["top_10"][0]
        for key in ["rank", "sector", "crowding", "total_score", "color", "icon", "label", "metrics"]:
            assert key in item, f"缺少字段: {key}"


# =====================================================================
# 测试 3: 环比计算
# =====================================================================
class TestCalcMom:
    def test_positive_change(self):
        assert si._calc_mom(60, 50) == "+20.0%"

    def test_negative_change(self):
        assert si._calc_mom(40, 50) == "-20.0%"

    def test_no_change(self):
        assert si._calc_mom(50, 50) == "+0.0%"

    def test_first_time(self):
        assert si._calc_mom(50, None) == "N/A(首次)"

    def test_prev_zero(self):
        assert si._calc_mom(50, 0) == "N/A(上次为0)"

    def test_prev_invalid(self):
        assert si._calc_mom(50, "abc") == "N/A(首次)"


# =====================================================================
# 测试 4: 向后兼容输出
# =====================================================================
class TestToRankedSector:
    def test_full_fields(self):
        scored = {
            "sector": "半导体", "heat": 70, "diffusion": 60, "volatility": 40,
            "rebound": 65, "crowding": 55, "total_score": 58.5,
            "fund_flow": 15e8, "change_pct": 2.5, "components": ["600584"],
            "mom_change": "+3.2%",
        }
        result = si._to_ranked_sector(scored)
        # 兼容字段
        assert result["sector"] == "半导体"
        assert result["heat_score"] == 58.5
        assert result["trend"] == "up"  # change_pct > 0
        assert result["components"] == ["600584"]
        # V2 新增字段
        assert "metrics" in result
        assert result["metrics"]["heat"] == 70
        assert result["mom_change"] == "+3.2%"
        # rationale 含五维数值
        assert "热度=70" in result["rationale"]
        assert "拥挤=55" in result["rationale"]

    def test_down_trend(self):
        scored = {
            "sector": "医药", "heat": 20, "diffusion": 30, "volatility": 50,
            "rebound": 0, "crowding": 10, "total_score": 20.0,
            "fund_flow": -5e8, "change_pct": -1.5, "components": [],
            "mom_change": "N/A(首次)",
        }
        result = si._to_ranked_sector(scored)
        assert result["trend"] == "down"

    def test_flat_trend(self):
        scored = {
            "sector": "地产", "heat": 30, "diffusion": 40, "volatility": 20,
            "rebound": 20, "crowding": 25, "total_score": 28.0,
            "fund_flow": 0, "change_pct": 0, "components": [],
            "mom_change": "+0.0%",
        }
        result = si._to_ranked_sector(scored)
        assert result["trend"] == "flat"


# =====================================================================
# 测试 5: 节点集成
# =====================================================================
class TestSectorInferenceNode:
    def test_node_returns_correct_keys(self):
        """测试节点返回 ranked_sectors + sector_crowding_map"""
        # mock 数据接口返回空 → 触发静态板块 fallback
        with patch.object(si.data_interface, "get_sector_data", return_value={"data": []}):
            # mock Bing 搜索返回空 (避免网络调用)
            with patch.object(si, "_fetch_attention_scores", return_value={}):
                # mock 历史分数文件不存在
                with patch.object(si, "_load_prev_scores", return_value=_FAKE_HISTORICAL):
                    with patch.object(si, "_save_scores") as mock_save:
                        node = si.create_sector_inference(
                            llm=None,
                            config={"sector_top_n": 5, "sector_crowding_top_n": 10},
                        )
                        result = node({"trade_date": "2026-07-11"})

        assert "ranked_sectors" in result
        assert "sector_crowding_map" in result
        assert isinstance(result["ranked_sectors"], list)
        assert isinstance(result["sector_crowding_map"], dict)
        # 静态板块有 8 个, top_n=5 → ranked_sectors 5 个
        assert len(result["ranked_sectors"]) == 5
        # 拥挤度地图取 top 10, 但静态只有 8 个
        assert len(result["sector_crowding_map"]["top_10"]) == 8
        # 分数应保存
        assert mock_save.called

    def test_node_total_score_weighted(self):
        """测试加权总分计算正确"""
        with patch.object(si.data_interface, "get_sector_data", return_value={"data": []}):
            with patch.object(si, "_fetch_attention_scores", return_value={}):
                with patch.object(si, "_load_prev_scores", return_value=_FAKE_HISTORICAL):
                    with patch.object(si, "_save_scores"):
                        node = si.create_sector_inference(
                            llm=None,
                            config={
                                "sector_top_n": 8,
                                "sector_crowding_top_n": 10,
                                "sector_metric_weights": {
                                    "heat": 0.25, "diffusion": 0.20,
                                    "volatility": 0.15, "rebound": 0.20,
                                    "crowding": 0.20,
                                },
                            },
                        )
                        result = node({"trade_date": "2026-07-11"})

        # 至少有一个板块, total_score 是五维加权
        assert len(result["ranked_sectors"]) > 0
        first = result["ranked_sectors"][0]
        m = first["metrics"]
        expected = round(
            0.25 * m["heat"] + 0.20 * m["diffusion"] + 0.15 * m["volatility"]
            + 0.20 * m["rebound"] + 0.20 * m["crowding"], 2
        )
        assert first["heat_score"] == expected

    def test_node_sorted_descending(self):
        """测试 ranked_sectors 按总分降序"""
        with patch.object(si.data_interface, "get_sector_data", return_value={"data": []}):
            with patch.object(si, "_fetch_attention_scores", return_value={}):
                with patch.object(si, "_load_prev_scores", return_value=_FAKE_HISTORICAL):
                    with patch.object(si, "_save_scores"):
                        node = si.create_sector_inference(llm=None, config={"sector_top_n": 8})
                        result = node({"trade_date": "2026-07-11"})

        scores = [s["heat_score"] for s in result["ranked_sectors"]]
        assert scores == sorted(scores, reverse=True), f"未降序: {scores}"

    def test_node_empty_when_no_history(self):
        """无历史数据时节点返回空 (PRD 10.2 降级为黑马-only)"""
        with patch.object(si.data_interface, "get_sector_data", return_value={"data": []}):
            with patch.object(si, "_fetch_attention_scores", return_value={}):
                with patch.object(si, "_load_prev_scores", return_value={}):
                    with patch.object(si, "_save_scores") as mock_save:
                        node = si.create_sector_inference(
                            llm=None, config={"sector_top_n": 5},
                        )
                        result = node({"trade_date": "2026-07-11"})

        assert result["ranked_sectors"] == []
        assert result["sector_crowding_map"] == {}
        # 无可保存数据, _save_scores 不应被调用
        assert not mock_save.called


# =====================================================================
# 测试 6: 工具函数
# =====================================================================
class TestUtilities:
    def test_safe_float_normal(self):
        assert si._safe_float(3.14) == 3.14
        assert si._safe_float("2.5") == 2.5

    def test_safe_float_none(self):
        assert si._safe_float(None) == 0.0
        assert si._safe_float("") == 0.0

    def test_safe_float_invalid(self):
        assert si._safe_float("abc") == 0.0
        assert si._safe_float(float("nan")) == 0.0

    def test_safe_float_custom_default(self):
        assert si._safe_float(None, default=-1.0) == -1.0

    def test_normalize_positive_normal(self):
        assert si._normalize_positive(25e8, 50e8) == 0.5

    def test_normalize_positive_capped(self):
        assert si._normalize_positive(100e8, 50e8) == 1.0  # 上限 1.0

    def test_normalize_positive_negative(self):
        assert si._normalize_positive(-10e8, 50e8) == 0.0  # 负数归 0

    def test_normalize_positive_zero_max(self):
        assert si._normalize_positive(10, 0) == 0.0  # max_val=0 归 0
