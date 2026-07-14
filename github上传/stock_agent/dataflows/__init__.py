"""
数据流模块 (对应 PRD 6.10 数据流图 / 6.3 权威数据源)

统一数据接口 + Provider 链 (AkShare 主, BaoStock/Tushare 备):
  - interface           : 统一数据接口 (函数式 API)
  - cache               : TTL 缓存装饰器
  - data_source_manager : 数据源管理器 (provider 链 + 故障转移)
  - providers/          : 各数据源 Provider 实现
    - akshare_provider  : AkShare (主数据源, 免费)
    - baostock_provider : BaoStock (备用, 免费)
    - tushare_provider  : Tushare (备用, 需 token)
    - cninfo_provider   : 巨潮资讯网 (公告检索)
    - policy/           : 政策信源 (V2 新增, 6 类权威信源)
"""
