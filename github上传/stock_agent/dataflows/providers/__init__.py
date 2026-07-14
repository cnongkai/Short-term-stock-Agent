"""
数据源 Provider 模块 (对应 PRD 6.10)

各数据源的具体实现, 被 DataSourceManager 按 provider 链调用:
  - akshare_provider  : AkShare 主数据源
  - baostock_provider : BaoStock 备用数据源
  - tushare_provider  : Tushare 备用数据源
  - cninfo_provider   : 巨潮资讯网公告检索
  - policy/           : 政策信源管理 (V2 新增)
"""
