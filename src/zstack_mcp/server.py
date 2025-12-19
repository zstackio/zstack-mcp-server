"""
ZStack MCP Server - 主入口

提供以下 MCP Tools:
1. search_api - 搜索 ZStack API
2. describe_api - 获取 API 详细说明
3. execute_api - 执行 ZStack API
4. search_metric - 搜索监控指标
5. get_metric_data - 获取监控数据

安全说明:
- 默认只允许调用只读 API（Query/Get/List 等）
- 设置环境变量 ZSTACK_ALLOW_ALL_API=true 可允许调用所有 API
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .api_search import ApiSearchIndex
from .metric_search import MetricSearchIndex
from .zstack_client import ZStackClient, ZStackApiError


# 初始化 MCP Server
mcp = FastMCP("zstack-mcp-server")

# 只读 API 前缀列表（安全的查询操作）
READONLY_API_PREFIXES = (
    'Query',      # 查询类
    'Get',        # 获取类
    'List',       # 列表类
    'Describe',   # 描述类
    'Check',      # 检查类
    'Count',      # 计数类
    'Search',     # 搜索类
    'Calculate',  # 计算类
    'Preview',    # 预览类
    'Validate',   # 验证类
    'Test',       # 测试类（如 TestConnection）
)


def is_readonly_api(api_name: str) -> bool:
    """检查是否为只读 API"""
    return api_name.startswith(READONLY_API_PREFIXES)


def is_write_api_allowed() -> bool:
    """检查是否允许调用写操作 API"""
    env_value = os.environ.get('ZSTACK_ALLOW_ALL_API', '').lower()
    return env_value in ('true', '1', 'yes', 'on')

# 全局索引和客户端
_api_index: Optional[ApiSearchIndex] = None
_metric_index: Optional[MetricSearchIndex] = None
_zstack_client: Optional[ZStackClient] = None


def get_data_dir() -> Path:
    """获取数据目录路径"""
    # 首先尝试相对于当前文件的路径
    current_dir = Path(__file__).parent
    
    # 尝试多个可能的路径
    possible_paths = [
        current_dir.parent.parent / "data",  # src/zstack_mcp/../../data
        current_dir.parent.parent.parent / "data",  # 更上一级
        Path.cwd() / "data",  # 当前工作目录下的 data
    ]
    
    for path in possible_paths:
        if path.exists():
            return path
    
    # 默认返回第一个路径
    return possible_paths[0]


def get_api_index() -> ApiSearchIndex:
    """获取 API 搜索索引（懒加载）"""
    global _api_index
    if _api_index is None:
        _api_index = ApiSearchIndex()
        data_dir = get_data_dir()
        api_docs_path = data_dir / "api_docs.json"
        if api_docs_path.exists():
            _api_index.load_from_file(api_docs_path)
        else:
            raise FileNotFoundError(f"找不到 API 文档文件: {api_docs_path}")
    return _api_index


def get_metric_index() -> MetricSearchIndex:
    """获取监控指标搜索索引（懒加载）"""
    global _metric_index
    if _metric_index is None:
        _metric_index = MetricSearchIndex()
        data_dir = get_data_dir()
        metric_path = data_dir / "zs_all_metric_metadata.json"
        if metric_path.exists():
            _metric_index.load_from_file(metric_path)
        else:
            raise FileNotFoundError(f"找不到监控指标文件: {metric_path}")
    return _metric_index


def get_zstack_client() -> ZStackClient:
    """获取 ZStack 客户端（懒加载）"""
    global _zstack_client
    if _zstack_client is None:
        _zstack_client = ZStackClient()
    return _zstack_client


# ============== MCP Tools ==============


@mcp.tool()
async def search_api(
    keywords: list[str],
    category: Optional[str] = None,
    limit: int = 15,
) -> str:
    """
    根据关键词搜索 ZStack API
    
    Args:
        keywords: 搜索关键词列表，如 ["Query", "Vm"] 或 ["Create", "Volume"]
                  支持驼峰拆分匹配，如搜索 "vm" 可以匹配 "QueryVmInstance"
        category: 可选，按分类过滤，如 "vm", "volume", "network"
        limit: 最多返回数量，默认 15
        
    Returns:
        匹配的 API 列表，包含名称、描述、分类、调用类型
    """
    try:
        index = get_api_index()
        results = index.search(keywords, category=category, limit=limit)
        
        if not results:
            return json.dumps({
                "success": True,
                "message": f"未找到匹配关键词 {keywords} 的 API",
                "apis": [],
                "hint": f"可用分类: {', '.join(index.list_categories()[:10])}..."
            }, ensure_ascii=False, indent=2)
        
        return json.dumps({
            "success": True,
            "count": len(results),
            "apis": results,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def describe_api(api_name: str) -> str:
    """
    获取指定 ZStack API 的详细参数说明
    
    Args:
        api_name: API 名称，如 "QueryVmInstance"
        
    Returns:
        API 的精简信息。对于 Query API，仅返回核心参数和 queryableFields。
    """
    try:
        index = get_api_index()
        detail = index.get_api_detail(api_name)
        
        if not detail:
            # 尝试模糊搜索给出建议
            suggestions = index.search([api_name], limit=5)
            return json.dumps({
                "success": False,
                "error": f"未找到 API: {api_name}",
                "suggestions": [s['name'] for s in suggestions] if suggestions else [],
            }, ensure_ascii=False, indent=2)
        
        return json.dumps({
            "success": True,
            "api": detail,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def execute_api(api_name: str, parameters: dict) -> str:
    """
    执行 ZStack API
    
    注意: 默认只允许调用只读 API（Query/Get/List 等）。
    如需调用写操作 API，请设置环境变量 ZSTACK_ALLOW_ALL_API=true
    
    Args:
        api_name: API 名称，如 "QueryVmInstance"
        parameters: API 参数字典
                   对于 Query API，conditions 格式为:
                   [{"name": "字段名", "op": "操作符", "value": "值"}, ...]
                   
    Returns:
        API 执行结果 (JSON 格式)
        
    Example:
        execute_api(
            api_name="QueryVmInstance",
            parameters={
                "conditions": [
                    {"name": "uuid", "op": "?=", "value": "ae6e57a0%"}
                ]
            }
        )
    """
    try:
        # 权限检查：默认只允许只读 API
        if not is_readonly_api(api_name) and not is_write_api_allowed():
            return json.dumps({
                "success": False,
                "error": f"安全限制: API '{api_name}' 是写操作，默认被禁止",
                "hint": "如需执行写操作，请设置环境变量 ZSTACK_ALLOW_ALL_API=true",
                "allowed_prefixes": list(READONLY_API_PREFIXES),
            }, ensure_ascii=False, indent=2)
        
        # 获取 API 信息
        index = get_api_index()
        api_info = index.get_api(api_name)
        
        if not api_info:
            return json.dumps({
                "success": False,
                "error": f"未找到 API: {api_name}",
            }, ensure_ascii=False, indent=2)
        
        # 获取客户端并执行
        client = get_zstack_client()
        is_async = api_info.call_type == 'async'
        
        result = await client.execute(
            api_name=api_name,
            full_api_name=api_info.full_name,
            parameters=parameters,
            is_async=is_async,
        )
        
        return json.dumps({
            "success": True,
            "result": result,
        }, ensure_ascii=False, indent=2)
        
    except ZStackApiError as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "code": e.code,
            "details": e.details,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_metric(
    keywords: list[str],
    namespace: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    搜索可用的 ZStack 监控指标
    
    Args:
        keywords: 搜索关键词，如 ["CPU", "Usage"] 或 ["Memory"]
                  支持驼峰拆分匹配
        namespace: 可选，按命名空间过滤，如 "ZStack/VM", "ZStack/Host"
        limit: 最多返回数量，默认 20
        
    Returns:
        匹配的监控指标列表，包含名称、描述、命名空间、可用标签
    """
    try:
        index = get_metric_index()
        results = index.search(keywords, namespace=namespace, limit=limit)
        
        if not results:
            return json.dumps({
                "success": True,
                "message": f"未找到匹配关键词 {keywords} 的监控指标",
                "metrics": [],
                "hint": f"可用命名空间: {', '.join(index.list_namespaces())}",
            }, ensure_ascii=False, indent=2)
        
        return json.dumps({
            "success": True,
            "count": len(results),
            "metrics": results,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_metric_data(
    namespace: str,
    metric_name: str,
    start_time: str,
    end_time: str,
    period: int = 60,
    labels: Optional[list[str]] = None,
) -> str:
    """
    获取 ZStack 监控数据
    
    Args:
        namespace: 命名空间，如 "ZStack/VM", "ZStack/Host"
        metric_name: 指标名称，如 "CPUUsedUtilization"
        start_time: 开始时间，ISO 格式，如 "2024-01-01T00:00:00Z"
        end_time: 结束时间，ISO 格式
        period: 采样周期(秒)，默认 60
        labels: 标签过滤列表，如 ["VMUuid=xxx", "HostUuid=yyy"]
        
    Returns:
        监控数据点列表
    """
    try:
        client = get_zstack_client()
        result = await client.query_metric_data(
            namespace=namespace,
            metric_name=metric_name,
            start_time=start_time,
            end_time=end_time,
            period=period,
            labels=labels,
        )
        
        return json.dumps({
            "success": True,
            "result": result,
        }, ensure_ascii=False, indent=2)
        
    except ZStackApiError as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "code": e.code,
            "details": e.details,
        }, ensure_ascii=False, indent=2)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


def main():
    """主入口函数"""
    # 使用 stdio 传输运行 MCP Server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

