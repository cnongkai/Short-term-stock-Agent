"""
测试数据源接口是否能成功获取数据
"""
import sys
import os
from datetime import datetime, timedelta

# 确保项目根目录在 Python 路径中
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from loguru import logger

# 配置日志
logger.remove()
logger.add(sys.stderr, level="INFO")


def test_akshare_provider():
    """测试 AkShare 数据源"""
    logger.info("=" * 60)
    logger.info("测试 AkShare 数据源...")
    logger.info("=" * 60)

    try:
        from stock_agent.dataflows.providers.akshare_provider import AkShareProvider

        provider = AkShareProvider()

        # 测试获取股票数据
        logger.info("\n1. 测试获取股票数据 (600584 长电科技)...")
        today = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        result = provider.get_stock_data("600584", start_date, today)
        if result and result.get("data"):
            logger.success(f"✓ 获取股票数据成功, 共 {len(result['data'])} 条记录")
            logger.info(f"  数据源: {result.get('source')}")
            if result['data']:
                logger.info(f"  最新记录: {result['data'][-1]}")
        else:
            logger.warning("✗ 获取股票数据失败或无数据")

        # 测试获取股票信息
        logger.info("\n2. 测试获取股票信息...")
        result = provider.get_stock_info("600584")
        if result and result.get("info"):
            logger.success("✓ 获取股票信息成功")
            logger.info(f"  股票名称: {result['info'].get('股票简称', 'N/A')}")
        else:
            logger.warning("✗ 获取股票信息失败或无数据")

        # 测试获取板块数据
        logger.info("\n3. 测试获取板块数据...")
        result = provider.get_sector_data()
        if result and result.get("data"):
            logger.success(f"✓ 获取板块数据成功, 共 {len(result['data'])} 个板块")
            logger.info(f"  前3个板块: {[s.get('板块名称', s.get('name', 'N/A')) for s in result['data'][:3]]}")
        else:
            logger.warning("✗ 获取板块数据失败或无数据")

        # 测试获取市场异动
        logger.info("\n4. 测试获取市场异动...")
        result = provider.get_market_movers()
        if result and result.get("data"):
            logger.success(f"✓ 获取市场异动成功, 共 {len(result['data'])} 只股票")
        else:
            logger.warning("✗ 获取市场异动失败或无数据")

        # 测试获取大盘指数
        logger.info("\n5. 测试获取大盘指数...")
        result = provider.get_market_index("sh000001")
        if result and result.get("data"):
            logger.success(f"✓ 获取大盘指数成功, 共 {len(result['data'])} 条记录")
            logger.info(f"  最新数据: {result['data'][-1] if result['data'] else 'N/A'}")
        else:
            logger.warning("✗ 获取大盘指数失败或无数据")

        logger.success("\n✓ AkShare 数据源测试完成")
        return True

    except Exception as e:
        logger.error(f"✗ AkShare 数据源测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_baostock_provider():
    """测试 BaoStock 数据源"""
    logger.info("\n" + "=" * 60)
    logger.info("测试 BaoStock 数据源...")
    logger.info("=" * 60)

    try:
        from stock_agent.dataflows.providers.baostock_provider import BaoStockProvider

        provider = BaoStockProvider()

        # 测试获取股票数据
        logger.info("\n1. 测试获取股票数据 (600584)...")
        today = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

        result = provider.get_stock_data("600584", start_date, today)
        if result and result.get("data"):
            logger.success(f"✓ 获取股票数据成功, 共 {len(result['data'])} 条记录")
            logger.info(f"  数据源: {result.get('source')}")
        else:
            logger.warning("✗ 获取股票数据失败或无数据")

        # 测试获取股票信息
        logger.info("\n2. 测试获取股票信息...")
        result = provider.get_stock_info("600584")
        if result and result.get("info"):
            logger.success("✓ 获取股票信息成功")
            logger.info(f"  股票名称: {result['info'].get('证券简称', 'N/A')}")
        else:
            logger.warning("✗ 获取股票信息失败或无数据")

        # 登出
        provider.logout()

        logger.success("\n✓ BaoStock 数据源测试完成")
        return True

    except Exception as e:
        logger.error(f"✗ BaoStock 数据源测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_data_source_manager():
    """测试数据源管理器"""
    logger.info("\n" + "=" * 60)
    logger.info("测试数据源管理器...")
    logger.info("=" * 60)

    try:
        from stock_agent.dataflows.data_source_manager import DataSourceManager

        config = {
            "akshare_enabled": True,
            "baostock_enabled": True,
            "tushare_enabled": False,  # 需要 token, 暂不测试
            "web_news_enabled": True,
            "cninfo_enabled": True,
        }

        manager = DataSourceManager(config)

        # 测试获取股票数据
        logger.info("\n1. 测试通过管理器获取股票数据...")
        today = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

        result = manager.get_stock_data("600584", start_date, today)
        if result and result.get("data"):
            logger.success(f"✓ 获取股票数据成功, 共 {len(result['data'])} 条记录")
            logger.info(f"  数据源: {result.get('source')}")
        else:
            logger.warning("✗ 获取股票数据失败或无数据")

        # 测试获取板块数据
        logger.info("\n2. 测试通过管理器获取板块数据...")
        result = manager.get_sector_data()
        if result and result.get("data"):
            logger.success(f"✓ 获取板块数据成功, 共 {len(result['data'])} 个板块")
        else:
            logger.warning("✗ 获取板块数据失败或无数据")

        logger.success("\n✓ 数据源管理器测试完成")
        return True

    except Exception as e:
        logger.error(f"✗ 数据源管理器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_module_import():
    """测试模块是否能正常导入"""
    logger.info("=" * 60)
    logger.info("测试模块导入...")
    logger.info("=" * 60)

    modules = [
        "akshare",
        "baostock",
        "tushare",
    ]

    for module_name in modules:
        try:
            __import__(module_name)
            logger.success(f"✓ {module_name} 模块已安装")
        except ImportError:
            logger.warning(f"✗ {module_name} 模块未安装")

    return True


if __name__ == "__main__":
    logger.info("\n" + "=" * 60)
    logger.info("开始测试数据源接口...")
    logger.info("=" * 60)

    # 测试模块导入
    test_module_import()

    # 测试各个数据源
    akshare_ok = test_akshare_provider()
    baostock_ok = test_baostock_provider()
    manager_ok = test_data_source_manager()

    # 总结
    logger.info("\n" + "=" * 60)
    logger.info("测试总结:")
    logger.info("=" * 60)
    logger.info(f"AkShare 数据源: {'✓ 正常' if akshare_ok else '✗ 失败'}")
    logger.info(f"BaoStock 数据源: {'✓ 正常' if baostock_ok else '✗ 失败'}")
    logger.info(f"数据源管理器: {'✓ 正常' if manager_ok else '✗ 失败'}")
    logger.info("=" * 60)