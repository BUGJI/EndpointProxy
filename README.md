# EndpointProxy

一种映射到 API 端点的 FRP（内网穿透）程序

## 简介

EndpointProxy 是一个基于 WebSocket 的反向代理工具，允许你将内网服务暴露到公网。它采用客户端 - 服务器架构，支持 HTTP/HTTPS 请求转发、流式响应、身份认证和权限控制。

## 特性

- 🔌 **WebSocket 长连接** - 客户端通过 WebSocket 与服务器保持持久连接
- 🌐 **HTTP 请求代理** - 支持任意 HTTP 方法（GET, POST, PUT, DELETE 等）
- ⚡ **流式响应支持** - 支持 SSE 等流式响应的实时传输
- 🔐 **身份认证** - 基于 HMAC 的客户端认证机制
- 🛡️ **权限控制** - 可配置每个客户端的访问路径权限
- 📊 **统计信息** - 内置请求统计和健康检查接口
- 🔄 **自动重连** - 客户端支持断线自动重连
- ❤️ **心跳保活** - 定期心跳检测连接状态

## 架构

```
┌─────────────┐     WebSocket      ┌─────────────┐
│   Client    │ ◄────────────────► │   Server    │
│ (内网服务)   │    ws://server:11435/ws          │ (公网)       │
│             │                    │             │
│ 本地服务     │                    │ HTTP API    │
│ :11434      │                    │ :11434      │
└─────────────┘                    └─────────────┘
                                          ▲
                                          │
                                   curl http://server:11434/{node_id}/path
```

## 安装

```bash
pip install -r requirements.txt
```

依赖项：
- `aiohttp` - 异步 HTTP/WebSocket 库
- `backoff` - 指数退避重试库

## 使用方法

### 1. 配置认证

#### INI 格式（推荐）

编辑 `auth_config.ini` 配置文件：

```ini
; 服务端认证配置文件 (INI 格式)
; 全局配置（可选）- 所有客户端共用的 auth_token
[global]
auth_token = your-global-auth-token

; 客户端配置 1 - 家庭 Ollama 节点
[client_home]
node_id = home-instance
auth_token = your-secret-key-change-this
permissions = *
description = Home instance

; 客户端配置 2 - 办公室节点
[client_office]
node_id = office-api
auth_token = another-secret-key
permissions = /api/*
description = Office API server with limited permissions
```

**配置说明：**
- `[global]` 节（可选）：设置全局 `auth_token`，所有客户端共用
- `[client_xxx]` 节：每个客户端一个配置节，必须以 `client_` 开头
  - `node_id`: 客户端唯一标识（必需）
  - `auth_token`: 认证令牌（如未设置则使用全局值）
  - `permissions`: 允许访问的路径，`*` 表示无限制，支持逗号分隔多个路径
  - `description`: 客户端描述信息

#### JSON 格式（兼容旧版）

编辑 `auth_config.json` 配置文件：

```json
{
    "clients": {
        "home-instance": {
            "secret": "your-secret-key-change-this",
            "permissions": ["*"],
            "description": "The Home instance"
        }
    }
}
```

- `secret`: 客户端认证密钥（**请修改为安全密钥**）
- `permissions`: 允许访问的路径，`["*"]` 表示无限制
- `description`: 客户端描述信息

### 2. 启动服务器

在公网服务器上运行：

```bash
python server.py --api-host 0.0.0.0 --api-port 11434 \
                 --client-host 0.0.0.0 --client-port 11435 \
                 --auth-config auth_config.ini
```

参数说明：
- `--api-host`: API 服务监听地址（默认：0.0.0.0）
- `--api-port`: API 服务端口（默认：11434）
- `--client-host`: WebSocket 服务监听地址（默认：0.0.0.0）
- `--client-port`: WebSocket 服务端口（默认：11435）
- `--auth-config`: 认证配置文件路径（默认：auth_config.ini，支持 .ini 和 .json 格式）

### 3. 启动客户端

#### 单连接模式（原有方式）

在内网机器上运行：

```bash
python client.py --node-id home-instance \
                 --auth-token your-secret-key-change-this \
                 --server-ws ws://your-server-ip:11435/ws \
                 --local-server http://127.0.0.1:11434
```

参数说明：
- `--node-id`: 客户端唯一标识（需与 auth_config.json 中的 key 匹配）
- `--auth-token`: 认证令牌（需与 auth_config.json 中的 secret 匹配）
- `--server-ws`: 服务器 WebSocket 地址
- `--local-server`: 本地服务地址（默认：http://127.0.0.1:11434）
- `--heartbeat`: 心跳间隔（秒，默认：15）
- `--reconnect-delay`: 重连延迟（秒，默认：5）

#### 多连接模式（INI 配置文件方式 - 推荐）

创建客户端配置文件 `client_config.ini`：

```ini
; 客户端多连接配置文件 (INI 格式)
; 全局配置（可选）- 所有连接共用的配置
[global]
auth_token = your-global-auth-token
server_ws = ws://127.0.0.1:11435/ws

; 连接配置 1 - 家庭 Ollama 节点
[connection_home]
node_id = home-ollama
local_server = http://127.0.0.1:11434
heartbeat_interval = 15
reconnect_delay = 5
enabled = true
description = Home Ollama Server

; 连接配置 2 - 办公室 Ollama 节点
[connection_office]
node_id = office-ollama
local_server = http://192.168.1.100:11434
heartbeat_interval = 20
reconnect_delay = 10
enabled = true
description = Office Ollama Server

; 连接配置 3 - 备用节点（已禁用）
[connection_backup]
node_id = backup-ollama
local_server = http://192.168.1.200:11434
enabled = false
description = Backup Ollama Server (disabled)
```

**配置说明：**
- `[global]` 节（可选）：设置全局共用配置
  - `auth_token`: 全局认证令牌，所有连接共用（如各连接有独立 token 可不设）
  - `server_ws`: 全局 WebSocket 服务器地址
- `[connection_xxx]` 节：每个连接一个配置节，必须以 `connection_` 开头
  - `node_id`: 客户端唯一标识（必需）
  - `auth_token`: 认证令牌（可选，如未设置则使用全局值）
  - `server_ws`: WebSocket 服务器地址（可选，如未设置则使用全局值）
  - `local_server`: 本地服务地址（可选，默认：http://127.0.0.1:11434）
  - `heartbeat_interval`: 心跳间隔秒数（可选，默认：15）
  - `reconnect_delay`: 重连延迟秒数（可选，默认：5）
  - `enabled`: 是否启用此连接（可选，默认：true）
  - `description`: 连接描述信息（可选）

启动多连接客户端：

```bash
python client.py --config client_config.ini
```

此模式会同时启动配置文件中所有 `enabled = true` 的连接，每个连接独立运行、自动重连。

#### 多连接模式（JSON 配置文件方式 - 兼容旧版）

创建客户端配置文件 `client_config.json`：

```json
{
    "connections": [
        {
            "node_id": "home-ollama",
            "auth_token": "your-secret-key-change-this",
            "server_ws": "ws://your-server-ip:11435/ws",
            "local_server": "http://127.0.0.1:11434",
            "heartbeat_interval": 15,
            "reconnect_delay": 5,
            "enabled": true,
            "description": "Home Ollama instance"
        },
        {
            "node_id": "office-api",
            "auth_token": "another-secret-key",
            "server_ws": "ws://your-server-ip:11435/ws",
            "local_server": "http://127.0.0.1:8080",
            "heartbeat_interval": 20,
            "reconnect_delay": 10,
            "enabled": true,
            "description": "Office API server"
        }
    ]
}
```

配置项说明：
- `node_id`: 客户端唯一标识（必需）
- `auth_token`: 认证令牌（必需）
- `server_ws`: 服务器 WebSocket 地址（可选，默认：ws://127.0.0.1:11435/ws）
- `local_server`: 本地服务地址（可选，默认：http://127.0.0.1:11434）
- `heartbeat_interval`: 心跳间隔秒数（可选，默认：15）
- `reconnect_delay`: 重连延迟秒数（可选，默认：5）
- `enabled`: 是否启用此连接（可选，默认：true）
- `description`: 连接描述信息（可选）

启动多连接客户端：

```bash
python client.py --config client_config.json
```

此模式会同时启动配置文件中所有 `enabled: true` 的连接，每个连接独立运行、自动重连。

### 4. 访问服务

通过服务器访问内网服务：

```bash
# 基本请求
curl http://your-server-ip:11434/home-instance/api/path

# POST 请求
curl -X POST http://your-server-ip:11434/home-instance/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model": "llama2", "messages": [...]}'

# 流式请求（SSE）
curl http://your-server-ip:11434/home-instance/v1/chat/completions \
     -d '{"stream": true}'
```

## 使用场景示例

### 单客户端场景

将本地的 Ollama 服务暴露到公网：

```bash
# 客户端（运行在有 Ollama 的机器上）
python client.py --node-id ollama-home \
                 --auth-token my-secret \
                 --server-ws ws://server:11435/ws \
                 --local-server http://127.0.0.1:11434

# 访问（从任何地方）
curl http://server:11434/ollama-home/api/generate \
     -d '{"model": "llama2", "prompt": "Hello"}'
```

### 多客户端场景

在一台机器上同时代理多个本地服务：

```bash
# 配置文件 client_config.json 包含多个连接
python client.py --config client_config.json
```

适用场景：
- 同时暴露 Ollama、数据库管理界面、监控面板等多个服务
- 不同服务使用不同的 node_id 和权限配置
- 统一管理所有连接的启动和停止

### 管理接口

服务器提供以下管理接口：

### 健康检查
```bash
curl http://localhost:11434/health
```

返回：
```json
{
    "status": "ok",
    "clients_connected": 1,
    "stats": {
        "total_requests": 100,
        "active_connections": 1,
        "bytes_transferred": 1024000
    },
    "timestamp": "2024-01-01T12:00:00"
}
```

### 列出所有节点（JSON API）
```bash
curl http://localhost:11434/nodes
# 或者
curl http://localhost:11434/node/list
```

返回：
```json
{
    "nodes": [
        {
            "node_id": "home-instance",
            "info": {...},
            "connected_at": "2024-01-01T12:00:00",
            "last_seen": "2024-01-01T12:05:00",
            "uptime": 300
        }
    ],
    "total": 1
}
```

## 其他 HTTP 服务

同样适用于任何 HTTP 服务：
- Web API
- 数据库管理界面
- 监控面板
- 文件服务等

## 安全建议

1. **修改默认密钥** - 务必更改 `auth_config.json` 中的默认密钥
2. **使用 HTTPS/WSS** - 生产环境建议使用反向代理（如 Nginx）启用 TLS
3. **限制权限** - 为不同客户端配置最小必要权限
4. **防火墙配置** - 仅开放必要的端口
5. **配置文件安全** - `client_config.json` 包含敏感信息，请妥善保管并设置合适的文件权限

## 配置文件示例

### 服务器认证配置 (auth_config.ini) - 推荐

```ini
; 服务端认证配置文件 (INI 格式)
; 全局配置（可选）- 所有客户端共用的 auth_token
[global]
auth_token = your-global-auth-token

; 客户端配置 1 - 家庭 Ollama 节点
[client_home]
node_id = home-ollama
auth_token = home-secret-key-123
permissions = *
description = Home Ollama instance

; 客户端配置 2 - 办公室节点（有限权限）
[client_office]
node_id = office-api
auth_token = office-secret-key-456
permissions = /api/*
description = Office API server with limited permissions
```

### 服务器认证配置 (auth_config.json) - 兼容旧版

```json
{
    "clients": {
        "home-ollama": {
            "secret": "your-secret-key-change-this",
            "permissions": ["*"],
            "description": "Home Ollama instance"
        },
        "office-api": {
            "secret": "another-secret-key",
            "permissions": ["/api/*"],
            "description": "Office API server with limited permissions"
        }
    }
}
```

### 客户端多连接配置 (client_config.ini) - 推荐

```ini
; 客户端多连接配置文件 (INI 格式)
; 全局配置（可选）- 所有连接共用的配置
[global]
auth_token = your-global-auth-token
server_ws = ws://your-server-ip:11435/ws

; 连接配置 1 - 家庭 Ollama 节点
[connection_home]
node_id = home-ollama
local_server = http://127.0.0.1:11434
heartbeat_interval = 15
reconnect_delay = 5
enabled = true
description = Home Ollama instance

; 连接配置 2 - 办公室 API 服务
[connection_office]
node_id = office-api
local_server = http://127.0.0.1:8080
enabled = true
description = Office API server
```

### 客户端多连接配置 (client_config.json) - 兼容旧版

```json
{
    "connections": [
        {
            "node_id": "home-ollama",
            "auth_token": "your-secret-key-change-this",
            "server_ws": "ws://your-server-ip:11435/ws",
            "local_server": "http://127.0.0.1:11434",
            "heartbeat_interval": 15,
            "reconnect_delay": 5,
            "enabled": true,
            "description": "Home Ollama instance"
        },
        {
            "node_id": "office-api",
            "auth_token": "another-secret-key",
            "server_ws": "ws://your-server-ip:11435/ws",
            "local_server": "http://127.0.0.1:8080",
            "enabled": true,
            "description": "Office API server"
        }
    ]
}
```

## 日志示例

服务器启动日志：
```
2024-01-01 12:00:00 - __main__ - INFO - Loaded 1 clients from auth_config.json
2024-01-01 12:00:00 - __main__ - INFO - 🔌 Client WebSocket service on ws://0.0.0.0:11435/ws
2024-01-01 12:00:00 - __main__ - INFO - 🌐 API service running on http://0.0.0.0:11434
2024-01-01 12:00:00 - __main__ - INFO - ============================================================
2024-01-01 12:00:00 - __main__ - INFO - 🚀 Reverse Proxy Server Started Successfully!
```

客户端连接日志：
```
2024-01-01 12:00:05 - __main__ - INFO - Connecting to server at ws://server:11435/ws as home-instance
2024-01-01 12:00:05 - __main__ - INFO - Successfully registered as home-instance
```

## License

MIT
