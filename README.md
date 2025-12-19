# ZStack MCP Server

让 AI 能够动态查询和调用 ZStack Cloud 的 2000+ API 的 MCP Server。

## 功能特性

- **API 搜索**: 根据关键词搜索 ZStack API，支持模糊匹配
- **API 描述**: 获取 API 的详细参数说明
- **API 执行**: 执行 ZStack API 并返回结果
- **监控指标搜索**: 搜索可用的监控指标
- **监控数据获取**: 获取指定指标的监控数据

## 安装

```bash
# 使用 pip 安装
pip install -e .

# 或者使用 uv
uv pip install -e .
```

## 配置

设置以下环境变量：

```bash
export ZSTACK_API_URL="http://localhost:8080"  # ZStack API 地址
export ZSTACK_ALLOW_ALL_API="false"             # 是否允许写操作（可选，默认 false）

# 认证方式一：用户名密码（会自动登录获取 Session）
export ZSTACK_ACCOUNT="admin"                   # 账户名
export ZSTACK_PASSWORD="your-password"          # 密码（明文）

# 认证方式二：直接传入 SessionID（优先级更高，设置后忽略用户名密码）
export ZSTACK_SESSION_ID="your-session-uuid"    # 已有的 Session UUID
```

### 认证方式说明

| 方式 | 环境变量 | 说明 |
|------|----------|------|
| 用户名密码 | `ZSTACK_ACCOUNT` + `ZSTACK_PASSWORD` | 自动登录获取 Session |
| Session ID | `ZSTACK_SESSION_ID` | 直接使用已有 Session（优先级更高） |

> 💡 如果同时设置了 `ZSTACK_SESSION_ID` 和用户名密码，会优先使用 Session ID

### 安全说明

**默认情况下，只允许调用只读 API**，包括：
- `Query*` - 查询类
- `Get*` - 获取类  
- `List*` - 列表类
- `Describe*` - 描述类
- `Check*` - 检查类
- `Count*` - 计数类
- 其他只读操作...

如需调用写操作 API（如 `CreateVmInstance`、`DeleteVolume` 等），需要设置：
```bash
export ZSTACK_ALLOW_ALL_API="true"
```

⚠️ **警告**: 启用写操作后，AI 可以执行创建、删除、修改等危险操作，请谨慎使用！

## 使用方式

### 作为 MCP Server 运行

```bash
# 直接运行
python -m zstack_mcp.server

# 或使用入口点
zstack-mcp
```

### 在 Claude Desktop 中配置

在 `claude_desktop_config.json` 中添加：

**方式一：使用用户名密码**
```json
{
  "mcpServers": {
    "zstack": {
      "command": "python",
      "args": ["-m", "zstack_mcp.server"],
      "env": {
        "ZSTACK_API_URL": "http://your-zstack-server:8080",
        "ZSTACK_ACCOUNT": "admin",
        "ZSTACK_PASSWORD": "your-password",
        "ZSTACK_ALLOW_ALL_API": "false"
      }
    }
  }
}
```

**方式二：使用 Session ID**
```json
{
  "mcpServers": {
    "zstack": {
      "command": "python",
      "args": ["-m", "zstack_mcp.server"],
      "env": {
        "ZSTACK_API_URL": "http://your-zstack-server:8080",
        "ZSTACK_SESSION_ID": "your-session-uuid",
        "ZSTACK_ALLOW_ALL_API": "false"
      }
    }
  }
}
```

> 💡 将 `ZSTACK_ALLOW_ALL_API` 设为 `"true"` 可启用写操作（创建/删除/修改等）

## 可用工具

### 1. search_api

根据关键词搜索 ZStack API。

**参数**:
- `keywords` (list[str]): 搜索关键词，如 `["Query", "Vm"]`
- `category` (str, 可选): 按分类过滤
- `limit` (int, 默认 15): 最多返回数量

### 2. describe_api

获取指定 API 的详细参数说明。

**参数**:
- `api_name` (str): API 名称，如 `"QueryVmInstance"`

### 3. execute_api

执行 ZStack API。

**参数**:
- `api_name` (str): API 名称
- `parameters` (dict): API 参数

### 4. search_metric

搜索可用的监控指标。

**参数**:
- `keywords` (list[str]): 搜索关键词
- `namespace` (str, 可选): 按命名空间过滤

### 5. get_metric_data

获取监控数据。

**参数**:
- `namespace` (str): 命名空间
- `metric_name` (str): 指标名称
- `start_time` (str): 开始时间 (ISO 格式)
- `end_time` (str): 结束时间
- `period` (int, 默认 60): 采样周期(秒)
- `labels` (list[str]): 标签过滤

## Query API 条件语法

对于 Query 类 API，`conditions` 参数支持以下操作符：

| 操作符 | 含义 | 示例 |
|--------|------|------|
| `=` | 等于 | `name=test` |
| `!=` | 不等于 | `state!=Deleted` |
| `>` | 大于 | `cpuNum>4` |
| `>=` | 大于等于 | `memorySize>=1073741824` |
| `<` | 小于 | `createDate<2024-01-01` |
| `<=` | 小于等于 | |
| `?=` | 模糊匹配(LIKE) | `name?=%test%` |
| `!?=` | 模糊不匹配 | |
| `~=` | 正则匹配 | `name~=.*test.*` |
| `!~=` | 正则不匹配 | |
| `=null` | 为空 | `description=null` |
| `!=null` | 不为空 | |
| `in` | 在列表中 | `state?=Running,Stopped` |
| `not in` | 不在列表中 | `state!?=Deleted,Destroyed` |

**conditions 格式**:
```json
{
    "conditions": [
        {"name": "uuid", "op": "=", "value": "xxx"},
        {"name": "state", "op": "in", "value": "Running,Stopped"}
    ]
}
```

## 示例交互

用户问: "帮我查一下 UUID 为 ae6e57a0 开头的 VM 的详情"

AI 会:
1. 调用 `search_api(keywords=["Query", "Vm", "Instance"])`
2. 调用 `describe_api(api_name="QueryVmInstance")`
3. 调用 `execute_api(api_name="QueryVmInstance", parameters={"conditions": [{"name": "uuid", "op": "?=", "value": "ae6e57a0%"}]})`

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
```

## License

MIT

