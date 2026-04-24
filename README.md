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
                 --auth-config auth_config.json
```

参数说明：
- `--api-host`: API 服务监听地址（默认：0.0.0.0）
- `--api-port`: API 服务端口（默认：11434）
- `--client-host`: WebSocket 服务监听地址（默认：0.0.0.0）
- `--client-port`: WebSocket 服务端口（默认：11435）
- `--auth-config`: 认证配置文件路径（默认：auth_config.json）

### 3. 启动客户端

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

## 管理接口

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

### 查看节点列表
```bash
curl http://localhost:11434/nodes
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

## 使用场景

### Ollama 远程访问
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

### 其他 HTTP 服务
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
