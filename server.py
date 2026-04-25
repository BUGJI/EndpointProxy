#!/usr/bin/env python3
import asyncio
import json
import logging
import hashlib
import hmac
import time
import signal
from typing import Dict, Optional, Set
from datetime import datetime
from pathlib import Path
import aiohttp
from aiohttp import web, ClientSession
import uuid
import tomli
import tomli_w

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AuthManager:
    """TOML 格式的认证管理器（参考 frp 配置结构）"""
    
    def __init__(self, config_file: str = "auth_config.toml"):
        self.config_file = config_file
        self.clients: Dict[str, dict] = {}
        self.global_auth_token = ''
        # Web 面板管理员账号密码
        self.admin_username = ''
        self.admin_password = ''
        self.load_config()
    
    def load_config(self):
        """加载 TOML 格式配置"""
        config_path = Path(self.config_file)
        if not config_path.exists():
            # 创建默认配置
            default_config = {
                'global': {
                    'auth_token': 'your-global-auth-token',
                    'admin_username': 'admin',
                    'admin_password': 'admin123'
                },
                'clients': {
                    'home-ollama': {
                        'secret': 'your-secret-key-change-this',
                        'permissions': ['*'],
                        'description': 'Home Ollama instance'
                    }
                }
            }
            self.save_config(default_config)
            self.clients = default_config['clients']
            self.global_auth_token = default_config['global']['auth_token']
            self.admin_username = default_config['global']['admin_username']
            self.admin_password = default_config['global']['admin_password']
            logger.warning(f"Created default config file: {self.config_file}")
            logger.warning(f"PLEASE CHANGE THE DEFAULT ADMIN PASSWORD AND SECRET KEY!")
            return
        
        # 检测文件格式，只支持 TOML
        suffix = config_path.suffix.lower()
        
        if suffix == '.toml':
            self._load_toml_config(config_path)
        else:
            logger.error(f"Unsupported config format: {suffix}. Only .toml is supported.")
            raise ValueError(f"Only TOML format (.toml) is supported. Got: {suffix}")
    
    def _load_toml_config(self, config_path: Path):
        """加载 TOML 格式配置（参考 frp 结构）"""
        try:
            with open(config_path, 'rb') as f:
                config = tomli.load(f)
            
            # 读取全局配置
            if 'global' in config:
                global_cfg = config['global']
                self.global_auth_token = global_cfg.get('auth_token', '')
                self.admin_username = global_cfg.get('admin_username', '')
                self.admin_password = global_cfg.get('admin_password', '')
                logger.info(f"Loaded global config: admin_username={self.admin_username}")
            
            # 读取客户端配置
            # 支持两种格式：
            # 1. [[clients]] 数组格式
            # 2. [clients.xxx] 字典格式
            if 'clients' in config:
                clients_cfg = config['clients']
                
                # 如果是数组格式 [[clients]]
                if isinstance(clients_cfg, list):
                    for client in clients_cfg:
                        node_id = client.get('node_id', '')
                        if not node_id:
                            logger.warning("Skipping client entry: missing node_id")
                            continue
                        self.clients[node_id] = {
                            'secret': client.get('secret', self.global_auth_token),
                            'permissions': client.get('permissions', ['*']),
                            'description': client.get('description', f'Client: {node_id}')
                        }
                        logger.info(f"Loaded client: {node_id}")
                
                # 如果是字典格式 [clients.xxx]
                elif isinstance(clients_cfg, dict):
                    for node_id, client_data in clients_cfg.items():
                        self.clients[node_id] = {
                            'secret': client_data.get('secret', self.global_auth_token),
                            'permissions': client_data.get('permissions', ['*']),
                            'description': client_data.get('description', f'Client: {node_id}')
                        }
                        logger.info(f"Loaded client: {node_id} ({client_data.get('description', '')})")
            
            logger.info(f"Loaded {len(self.clients)} clients from TOML config")
        except Exception as e:
            logger.error(f"Failed to load TOML config: {e}")
            raise
    
    def save_config(self, config: dict):
        """保存为 TOML 格式配置"""
        config_path = Path(self.config_file)
        
        # 确保是 TOML 格式
        if config_path.suffix.lower() != '.toml':
            config_path = config_path.with_suffix('.toml')
        
        output_config = {}
        
        # 全局配置
        if self.admin_username or self.global_auth_token:
            output_config['global'] = {
                'auth_token': self.global_auth_token,
                'admin_username': self.admin_username,
                'admin_password': self.admin_password
            }
        
        # 客户端配置（使用字典格式）
        if 'clients' in config:
            output_config['clients'] = {}
            for node_id, data in config['clients'].items():
                output_config['clients'][node_id] = {
                    'secret': data.get('secret', ''),
                    'permissions': data.get('permissions', ['*']),
                    'description': data.get('description', '')
                }
        
        with open(config_path, 'wb') as f:
            tomli_w.dump(output_config, f)
        
        logger.info(f"Saved config to {config_path}")
    
    def authenticate(self, node_id: str, auth_token: str) -> bool:
        """验证客户端"""
        if node_id not in self.clients:
            return False
        expected = self.clients[node_id]['secret']
        return hmac.compare_digest(auth_token, expected)
    
    def check_permission(self, node_id: str, path: str) -> bool:
        """检查路径权限"""
        if node_id not in self.clients:
            return False
        
        permissions = self.clients[node_id].get('permissions', [])
        if '*' in permissions:
            return True
        
        for pattern in permissions:
            if pattern.endswith('*'):
                if path.startswith(pattern[:-1]):
                    return True
            elif pattern == path:
                return True
        return False

class ReverseProxyServer:
    def __init__(self, 
                 client_host: str = "0.0.0.0", 
                 client_port: int = 11435,
                 api_host: str = "0.0.0.0", 
                 api_port: int = 11434,
                 auth_config: str = "auth_config.toml",
                 panel_path: str = "/"):
        
        self.client_host = client_host
        self.client_port = client_port
        self.api_host = api_host
        self.api_port = api_port
        self.panel_path = panel_path.rstrip('/') if panel_path != '/' else ''
        
        self.auth = AuthManager(auth_config)
        self.clients: Dict[str, dict] = {}
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.request_counter = 0
        
        # 统计信息
        self.stats = {
            'total_requests': 0,
            'active_connections': 0,
            'bytes_transferred': 0
        }
        
        # 运行状态
        self.client_runner = None
        self.api_runner = None
        self.running = False
    
    async def handle_client_websocket(self, request: web.Request):
        """处理客户端的WebSocket连接"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        node_id = None
        authenticated = False
        
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data.get("type") == "register":
                        node_id = data.get("node_id")
                        auth_token = data.get("auth_token", "")
                        
                        if not node_id or not self.auth.authenticate(node_id, auth_token):
                            logger.warning(f"Authentication failed for {node_id}")
                            await ws.send_json({"type": "error", "message": "Authentication failed"})
                            await ws.close()
                            return ws
                        
                        authenticated = True
                        self.stats['active_connections'] += 1
                        
                        # 处理旧连接
                        if node_id in self.clients:
                            logger.info(f"Client {node_id} reconnecting, closing old connection")
                            old_ws = self.clients[node_id].get("websocket")
                            if old_ws and not old_ws.closed:
                                await old_ws.close()
                        
                        self.clients[node_id] = {
                            "websocket": ws,
                            "info": data.get("info", {}),
                            "last_seen": datetime.now(),
                            "connected_at": datetime.now(),
                            "auth_token": auth_token
                        }
                        
                        logger.info(f"✅ Client {node_id} authenticated and registered")
                        await ws.send_json({"type": "registered", "node_id": node_id})
                    
                    elif data.get("type") == "response" and authenticated:
                        # 处理普通响应
                        request_id = data.get("request_id")
                        response_data = data.get("data")
                        
                        if request_id and request_id in self.pending_requests:
                            future = self.pending_requests[request_id]
                            if not future.done():
                                future.set_result(response_data)
                                logger.debug(f"Received response for {request_id}")
                    
                    elif data.get("type") == "heartbeat" and authenticated:
                        # 心跳
                        if node_id and node_id in self.clients:
                            self.clients[node_id]["last_seen"] = datetime.now()
                            await ws.send_json({"type": "heartbeat_ack"})
                    
                    elif data.get("type") == "error" and authenticated:
                        logger.error(f"Client error: {data.get('message')}")
                        
        except Exception as e:
            logger.error(f"Error with client {node_id}: {e}")
        finally:
            if node_id and node_id in self.clients:
                logger.info(f"Client {node_id} disconnected")
                del self.clients[node_id]
                self.stats['active_connections'] -= 1
            
            if not ws.closed:
                await ws.close()
        
        return ws
    
    async def proxy_request(self, node_id: str, request: web.Request, path: str):
        """代理HTTP请求到客户端"""
        if node_id not in self.clients:
            return web.json_response({"error": f"Node {node_id} not connected"}, status=404)
        
        # 检查权限
        if not self.auth.check_permission(node_id, path):
            logger.warning(f"Permission denied for {node_id} to access {path}")
            return web.json_response({"error": "Permission denied"}, status=403)
        
        ws = self.clients[node_id]["websocket"]
        if ws.closed:
            return web.json_response({"error": f"Node {node_id} connection closed"}, status=503)
        
        # 生成请求ID
        self.request_counter += 1
        request_id = f"{node_id}-{self.request_counter}-{uuid.uuid4().hex[:8]}"
        
        # 读取请求体
        body_str = None
        if request.can_read_body:
            try:
                body_bytes = await request.read()
                body_str = body_bytes.decode('utf-8', errors='replace')
                self.stats['bytes_transferred'] += len(body_bytes)
            except Exception as e:
                logger.error(f"Error reading body: {e}")
        
        # 构建请求数据
        request_data = {
            "type": "request",
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "headers": dict(request.headers),
            "body": body_str
        }
        
        # 创建Future用于等待响应
        future = asyncio.Future()
        self.pending_requests[request_id] = future
        
        try:
            # 发送请求到客户端
            await ws.send_json(request_data)
            logger.debug(f"Sent request {request_id} to client {node_id}")
            
            # 等待客户端响应（非流式）
            response = await asyncio.wait_for(future, timeout=300.0)
            
            # 构建HTTP响应
            status = response.get("status", 200)
            headers = response.get("headers", {})
            body_content = response.get("body", "")
            
            # 移除可能导致问题的headers
            headers.pop('content-length', None)
            headers.pop('transfer-encoding', None)
            
            self.stats['total_requests'] += 1
            self.stats['bytes_transferred'] += len(body_content)
            
            return web.Response(
                status=status,
                headers=headers,
                text=body_content
            )
            
        except asyncio.TimeoutError:
            logger.error(f"Request {request_id} to client {node_id} timed out")
            return web.json_response({"error": "Request timeout"}, status=504)
        except Exception as e:
            logger.error(f"Error proxying request to {node_id}: {e}")
            return web.json_response({"error": str(e)}, status=500)
        finally:
            if request_id in self.pending_requests:
                del self.pending_requests[request_id]
    
    async def handle_api_request(self, request: web.Request):
        """处理所有API请求"""
        # 特殊路径：管理接口
        if request.path == '/health':
            return web.json_response({
                "status": "ok",
                "clients_connected": len(self.clients),
                "stats": self.stats,
                "timestamp": datetime.now().isoformat()
            })
        
        if request.path == '/nodes' or request.path == '/node/list':
            nodes = []
            for node_id, data in self.clients.items():
                nodes.append({
                    "node_id": node_id,
                    "info": data.get("info", {}),
                    "connected_at": data.get("connected_at").isoformat(),
                    "last_seen": data.get("last_seen").isoformat(),
                    "uptime": (datetime.now() - data.get("connected_at")).seconds
                })
            return web.json_response({"nodes": nodes, "total": len(nodes)})
        
        # Web 面板管理接口
        api_prefix = panel_base + '/api/panel' if panel_base else '/api/panel'
        if request.path == api_prefix + '/login':
            return await self.handle_panel_login(request)
        if request.path == api_prefix + '/nodes':
            return await self.handle_panel_nodes(request)
        if request.path == api_prefix + '/keys':
            return await self.handle_panel_keys(request)
        if request.path.startswith(api_prefix + '/key/'):
            return await self.handle_panel_key_operation(request)

        # 解析路径: /{node_id}/{path}
        path_parts = request.path.lstrip('/').split('/', 1)
        
        if len(path_parts) < 1:
            return web.json_response({"error": "Invalid path, expected /{node_id}/..."}, status=400)
        
        node_id = path_parts[0]
        
        # 检查节点ID格式
        if not node_id or not all(c.isalnum() or c in '-_' for c in node_id):
            return web.json_response({"error": "Invalid node_id format"}, status=400)
        
        # 剩余路径
        remaining_path = '/' + path_parts[1] if len(path_parts) > 1 else '/'
        
        # 代理请求
        return await self.proxy_request(node_id, request, remaining_path)
    
    async def serve_panel_html(self, request: web.Request):
        """提供 Web 面板 HTML 页面"""
        html_content = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>反向代理管理面板</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; }
        .header h1 { font-size: 24px; margin-bottom: 10px; }
        .login-form { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; margin: 50px auto; }
        .login-form h2 { margin-bottom: 20px; color: #333; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #555; }
        .form-group input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; }
        .btn { background: #667eea; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 14px; }
        .btn:hover { background: #5a6fd6; }
        .btn-danger { background: #e74c3c; }
        .btn-danger:hover { background: #c0392b; }
        .btn-success { background: #27ae60; }
        .btn-success:hover { background: #219a52; }
        .panel { display: none; }
        .panel.active { display: block; }
        .card { background: white; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .card h3 { margin-bottom: 15px; color: #333; border-bottom: 2px solid #667eea; padding-bottom: 10px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; color: #333; font-weight: 600; }
        tr:hover { background: #f8f9fa; }
        .status-online { color: #27ae60; font-weight: bold; }
        .status-offline { color: #e74c3c; font-weight: bold; }
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; }
        .modal.active { display: flex; align-items: center; justify-content: center; }
        .modal-content { background: white; padding: 30px; border-radius: 10px; max-width: 500px; width: 90%; }
        .modal-content h3 { margin-bottom: 20px; }
        .error-msg { color: #e74c3c; margin-top: 10px; display: none; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 20px; border-radius: 10px; text-align: center; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .stat-card .value { font-size: 28px; font-weight: bold; color: #667eea; }
        .stat-card .label { color: #666; margin-top: 5px; }
        .nav-tabs { display: flex; gap: 10px; margin-bottom: 20px; }
        .nav-tab { padding: 10px 20px; background: #e9ecef; border: none; border-radius: 5px; cursor: pointer; }
        .nav-tab.active { background: #667eea; color: white; }
    </style>
</head>
<body>
    <div class="container">
        <!-- 登录表单 -->
        <div id="loginPanel" class="login-form">
            <h2>🔐 管理员登录</h2>
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="username" placeholder="请输入用户名">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="password" placeholder="请输入密码">
            </div>
            <button class="btn" onclick="login()">登录</button>
            <p class="error-msg" id="loginError"></p>
        </div>

        <!-- 主面板 -->
        <div id="mainPanel" class="panel">
            <div class="header">
                <h1>🚀 反向代理管理面板</h1>
                <p>欢迎，<span id="adminName"></span></p>
                <button class="btn" style="margin-top:10px;background:rgba(255,255,255,0.2);" onclick="logout()">退出</button>
            </div>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="value" id="statNodes">0</div>
                    <div class="label">在线节点</div>
                </div>
                <div class="stat-card">
                    <div class="value" id="statRequests">0</div>
                    <div class="label">总请求数</div>
                </div>
                <div class="stat-card">
                    <div class="value" id="statBytes">0</div>
                    <div class="label">传输数据</div>
                </div>
            </div>

            <div class="nav-tabs">
                <button class="nav-tab active" onclick="switchTab('nodes')">节点列表</button>
                <button class="nav-tab" onclick="switchTab('keys')">API 密钥管理</button>
            </div>

            <div id="tab-nodes" class="card">
                <h3>📡 在线节点</h3>
                <table>
                    <thead>
                        <tr><th>节点 ID</th><th>描述</th><th>状态</th><th>连接时间</th><th>最后活跃</th><th>运行时长</th></tr>
                    </thead>
                    <tbody id="nodesTable"></tbody>
                </table>
            </div>

            <div id="tab-keys" class="card" style="display:none;">
                <h3>🔑 API 密钥管理</h3>
                <button class="btn btn-success" onclick="showAddKeyModal()">+ 新增密钥</button>
                <table style="margin-top:15px;">
                    <thead>
                        <tr><th>节点 ID</th><th>密钥 (Secret)</th><th>权限</th><th>描述</th><th>操作</th></tr>
                    </thead>
                    <tbody id="keysTable"></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- 新增/编辑密钥模态框 -->
    <div id="keyModal" class="modal">
        <div class="modal-content">
            <h3 id="modalTitle">新增 API 密钥</h3>
            <input type="hidden" id="editNodeId">
            <div class="form-group">
                <label>节点 ID</label>
                <input type="text" id="nodeId" placeholder="唯一标识符">
            </div>
            <div class="form-group">
                <label>密钥 (Secret)</label>
                <input type="text" id="secretKey" placeholder="认证密钥">
            </div>
            <div class="form-group">
                <label>权限 (逗号分隔，* 表示全部)</label>
                <input type="text" id="permissions" value="*" placeholder="*, /api/*">
            </div>
            <div class="form-group">
                <label>描述</label>
                <input type="text" id="description" placeholder="可选描述">
            </div>
            <div style="display:flex;gap:10px;justify-content:flex-end;">
                <button class="btn" onclick="closeModal()">取消</button>
                <button class="btn btn-success" onclick="saveKey()">保存</button>
            </div>
        </div>
    </div>

    <script>
        let authToken = localStorage.getItem('panel_token');
        let adminUser = localStorage.getItem('panel_username');

        if (authToken && adminUser) {
            showMainPanel();
        }

        async function login() {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            const errorEl = document.getElementById('loginError');

            try {
                const res = await fetch('/api/panel/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username, password})
                });
                const data = await res.json();
                if (res.ok) {
                    authToken = data.token;
                    adminUser = data.username;
                    localStorage.setItem('panel_token', authToken);
                    localStorage.setItem('panel_username', adminUser);
                    showMainPanel();
                } else {
                    errorEl.textContent = data.error || '登录失败';
                    errorEl.style.display = 'block';
                }
            } catch (e) {
                errorEl.textContent = '网络错误';
                errorEl.style.display = 'block';
            }
        }

        function logout() {
            localStorage.removeItem('panel_token');
            localStorage.removeItem('panel_username');
            location.reload();
        }

        function showMainPanel() {
            document.getElementById('loginPanel').style.display = 'none';
            document.getElementById('mainPanel').classList.add('active');
            document.getElementById('adminName').textContent = adminUser;
            loadData();
            setInterval(loadData, 5000);
        }

        async function loadData() {
            await loadNodes();
            await loadStats();
            await loadKeys();
        }

        async function loadNodes() {
            try {
                const res = await fetch('/api/panel/nodes', {
                    headers: {'Authorization': 'Bearer ' + authToken}
                });
                const data = await res.json();
                const tbody = document.getElementById('nodesTable');
                tbody.innerHTML = '';
                document.getElementById('statNodes').textContent = data.nodes.length;
                data.nodes.forEach(node => {
                    const uptime = formatUptime(node.connected_at ? ((new Date() - new Date(node.connected_at))/1000) : 0);
                    tbody.innerHTML += `<tr>
                        <td>${escapeHtml(node.node_id)}</td>
                        <td>${escapeHtml(node.description || '-')}</td>
                        <td class="status-online">● 在线</td>
                        <td>${node.connected_at ? new Date(node.connected_at).toLocaleString() : '-'}</td>
                        <td>${node.last_seen ? new Date(node.last_seen).toLocaleString() : '-'}</td>
                        <td>${uptime}</td>
                    </tr>`;
                });
            } catch (e) { console.error(e); }
        }

        async function loadStats() {
            try {
                const res = await fetch('/health');
                const data = await res.json();
                document.getElementById('statRequests').textContent = data.stats.total_requests || 0;
                document.getElementById('statBytes').textContent = formatBytes(data.stats.bytes_transferred || 0);
            } catch (e) { console.error(e); }
        }

        async function loadKeys() {
            try {
                const res = await fetch('/api/panel/keys', {
                    headers: {'Authorization': 'Bearer ' + authToken}
                });
                const data = await res.json();
                const tbody = document.getElementById('keysTable');
                tbody.innerHTML = '';
                data.keys.forEach(key => {
                    tbody.innerHTML += `<tr>
                        <td>${escapeHtml(key.node_id)}</td>
                        <td><code>${escapeHtml(key.secret.substring(0,16))}...</code></td>
                        <td>${escapeHtml((key.permissions||[]).join(', '))}</td>
                        <td>${escapeHtml(key.description || '-')}</td>
                        <td>
                            <button class="btn" onclick="editKey('${escapeJs(key.node_id)}')">编辑</button>
                            <button class="btn btn-danger" onclick="deleteKey('${escapeJs(key.node_id)}')">删除</button>
                        </td>
                    </tr>`;
                });
            } catch (e) { console.error(e); }
        }

        function switchTab(tab) {
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('[class^="nav-tab"]').forEach((t,i) => {
                if((tab==='nodes' && i===0)||(tab==='keys' && i===1)) t.classList.add('active');
            });
            document.getElementById('tab-nodes').style.display = tab==='nodes'?'block':'none';
            document.getElementById('tab-keys').style.display = tab==='keys'?'block':'none';
        }

        function showAddKeyModal() {
            document.getElementById('modalTitle').textContent = '新增 API 密钥';
            document.getElementById('editNodeId').value = '';
            document.getElementById('nodeId').value = '';
            document.getElementById('secretKey').value = '';
            document.getElementById('permissions').value = '*';
            document.getElementById('description').value = '';
            document.getElementById('keyModal').classList.add('active');
        }

        function editKey(nodeId) {
            document.getElementById('modalTitle').textContent = '编辑 API 密钥';
            document.getElementById('editNodeId').value = nodeId;
            document.getElementById('nodeId').value = nodeId;
            document.getElementById('nodeId').disabled = true;
            document.getElementById('keyModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('keyModal').classList.remove('active');
            document.getElementById('nodeId').disabled = false;
        }

        async function saveKey() {
            const nodeId = document.getElementById('nodeId').value;
            const secret = document.getElementById('secretKey').value;
            const permissions = document.getElementById('permissions').value.split(',').map(s=>s.trim());
            const description = document.getElementById('description').value;
            const editId = document.getElementById('editNodeId').value;

            if (!nodeId || !secret) { alert('节点 ID 和密钥不能为空'); return; }

            try {
                const method = editId ? 'PUT' : 'POST';
                const url = editId ? `/api/panel/key/${encodeURIComponent(editId)}` : '/api/panel/keys';
                const res = await fetch(url, {
                    method,
                    headers: {
                        'Authorization': 'Bearer ' + authToken,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({node_id: nodeId, secret, permissions, description})
                });
                if (res.ok) {
                    closeModal();
                    loadKeys();
                } else {
                    const err = await res.json();
                    alert(err.error || '操作失败');
                }
            } catch (e) { alert('网络错误'); }
        }

        async function deleteKey(nodeId) {
            if (!confirm('确定要删除这个 API 密钥吗？')) return;
            try {
                const res = await fetch(`/api/panel/key/${encodeURIComponent(nodeId)}`, {
                    method: 'DELETE',
                    headers: {'Authorization': 'Bearer ' + authToken}
                });
                if (res.ok) loadKeys();
                else { const err = await res.json(); alert(err.error || '删除失败'); }
            } catch (e) { alert('网络错误'); }
        }

        function formatUptime(seconds) {
            if (!seconds) return '-';
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            return (d>0?d+'天 ':'') + h+'小时 '+m+'分';
        }

        function formatBytes(bytes) {
            if (!bytes) return '0 B';
            const k = 1024;
            const sizes = ['B','KB','MB','GB','TB'];
            const i = Math.floor(Math.log(bytes)/Math.log(k));
            return parseFloat((bytes/Math.pow(k,i)).toFixed(2))+' '+sizes[i];
        }

        function escapeHtml(str) {
            if (!str) return '';
            return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        }
        function escapeJs(str) {
            return String(str).replace(/'/g,"\\'").replace(/"/g,'\\"');
        }
    </script>
</body>
</html>'''
        return web.Response(text=html_content, content_type='text/html')

    async def check_admin_auth(self, request: web.Request) -> bool:
        """检查管理员认证"""
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return False
        
        token = auth_header[7:]
        import base64
        try:
            decoded = base64.b64decode(token).decode('utf-8')
            username, password = decoded.split(':', 1)
            return username == self.auth.admin_username and password == self.auth.admin_password
        except:
            return False
    
    async def handle_panel_login(self, request: web.Request):
        """处理面板登录"""
        if request.method != 'POST':
            return web.json_response({"error": "Method not allowed"}, status=405)
        
        try:
            data = await request.json()
            username = data.get('username', '')
            password = data.get('password', '')
            
            if not self.auth.admin_username or not self.auth.admin_password:
                return web.json_response({"error": "Admin credentials not configured"}, status=503)
            
            if username == self.auth.admin_username and password == self.auth.admin_password:
                import base64
                token = base64.b64encode(f"{username}:{password}".encode()).decode()
                return web.json_response({
                    "success": True,
                    "token": token,
                    "username": username
                })
            else:
                return web.json_response({"error": "Invalid credentials"}, status=401)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)
    
    async def handle_panel_nodes(self, request: web.Request):
        """处理面板节点列表请求"""
        if not await self.check_admin_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        if request.method != 'GET':
            return web.json_response({"error": "Method not allowed"}, status=405)
        
        nodes = []
        for node_id, data in self.clients.items():
            nodes.append({
                "node_id": node_id,
                "info": data.get("info", {}),
                "connected_at": data.get("connected_at").isoformat() if data.get("connected_at") else None,
                "last_seen": data.get("last_seen").isoformat() if data.get("last_seen") else None,
                "description": self.auth.clients.get(node_id, {}).get('description', ''),
                "permissions": self.auth.clients.get(node_id, {}).get('permissions', [])
            })
        return web.json_response({"nodes": nodes, "total": len(nodes)})
    
    async def handle_panel_keys(self, request: web.Request):
        """处理面板密钥管理请求（列出所有密钥）"""
        if not await self.check_admin_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        if request.method != 'GET':
            return web.json_response({"error": "Method not allowed"}, status=405)
        
        keys = []
        for node_id, data in self.auth.clients.items():
            keys.append({
                "node_id": node_id,
                "secret": data.get('secret', ''),
                "permissions": data.get('permissions', []),
                "description": data.get('description', '')
            })
        return web.json_response({"keys": keys, "total": len(keys)})
    
    async def handle_panel_key_operation(self, request: web.Request):
        """处理面板密钥增删改操作"""
        if not await self.check_admin_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        
        path_parts = request.path.rstrip('/').split('/')
        if len(path_parts) < 5:
            return web.json_response({"error": "Invalid path"}, status=400)
        
        node_id = path_parts[-1]
        
        if request.method == 'PUT':
            try:
                data = await request.json()
                secret = data.get('secret', '')
                permissions = data.get('permissions', ['*'])
                description = data.get('description', f'Client: {node_id}')
                
                if not secret:
                    import secrets
                    secret = secrets.token_urlsafe(32)
                
                self.auth.clients[node_id] = {
                    'secret': secret,
                    'permissions': permissions if isinstance(permissions, list) else [permissions],
                    'description': description
                }
                
                self.auth.save_config({'clients': self.auth.clients})
                logger.info(f"Updated/Added key for node: {node_id}")
                return web.json_response({
                    "success": True,
                    "node_id": node_id,
                    "secret": secret,
                    "message": "Key updated successfully"
                })
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        
        elif request.method == 'DELETE':
            if node_id not in self.auth.clients:
                return web.json_response({"error": "Node not found"}, status=404)
            
            del self.auth.clients[node_id]
            
            if node_id in self.clients:
                ws = self.clients[node_id].get("websocket")
                if ws and not ws.closed:
                    await ws.close()
                del self.clients[node_id]
            
            self.auth.save_config({'clients': self.auth.clients})
            logger.info(f"Deleted key for node: {node_id}")
            return web.json_response({
                "success": True,
                "node_id": node_id,
                "message": "Key deleted successfully"
            })
        
        elif request.method == 'POST':
            try:
                data = await request.json()
                if node_id not in self.auth.clients:
                    return web.json_response({"error": "Node not found"}, status=404)
                
                current = self.auth.clients[node_id]
                if 'secret' in data:
                    current['secret'] = data['secret']
                if 'permissions' in data:
                    perms = data['permissions']
                    current['permissions'] = perms if isinstance(perms, list) else [perms]
                if 'description' in data:
                    current['description'] = data['description']
                
                self.auth.clients[node_id] = current
                self.auth.save_config({'clients': self.auth.clients})
                logger.info(f"Modified key for node: {node_id}")
                return web.json_response({
                    "success": True,
                    "node_id": node_id,
                    "message": "Key modified successfully"
                })
            except Exception as e:
                return web.json_response({"error": str(e)}, status=400)
        
        else:
            return web.json_response({"error": "Method not allowed"}, status=405)

    async def start_api_service(self):
        """启动API服务"""
        app = web.Application()
        
        # 通用代理端点 - 匹配所有路径
        app.router.add_route('*', '/{path:.*}', self.handle_api_request)
        
        # CORS支持
        async def cors_middleware(app, handler):
            async def middleware(request):
                if request.method == 'OPTIONS':
                    return web.Response(
                        headers={
                            'Access-Control-Allow-Origin': '*',
                            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
                            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                        }
                    )
                response = await handler(request)
                response.headers['Access-Control-Allow-Origin'] = '*'
                return response
            return middleware
        
        app.middlewares.append(cors_middleware)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.api_host, self.api_port)
        await site.start()
        
        logger.info(f"🌐 API service running on http://{self.api_host}:{self.api_port}")
        return runner
    
    async def start_client_service(self):
        """启动客户端WebSocket服务"""
        app = web.Application()
        app.router.add_get('/ws', self.handle_client_websocket)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.client_host, self.client_port)
        await site.start()
        
        logger.info(f"🔌 Client WebSocket service on ws://{self.client_host}:{self.client_port}/ws")
        return runner
    
    async def start(self):
        """启动服务器"""
        self.client_runner = await self.start_client_service()
        self.api_runner = await self.start_api_service()
        self.running = True
        
        logger.info("=" * 60)
        logger.info("🚀 Reverse Proxy Server Started Successfully!")
        logger.info(f"📡 WebSocket endpoint: ws://{self.client_host}:{self.client_port}/ws")
        logger.info(f"🌐 HTTP endpoint: http://{self.api_host}:{self.api_port}")
        logger.info("📝 Usage: curl http://<server>:11434/<node_id>/any/path")
        logger.info("=" * 60)
    
    async def stop(self):
        """停止服务器"""
        self.running = False
        
        if self.client_runner:
            await self.client_runner.cleanup()
        if self.api_runner:
            await self.api_runner.cleanup()
        
        # 关闭所有客户端连接
        for client_data in self.clients.values():
            ws = client_data.get("websocket")
            if ws and not ws.closed:
                await ws.close()
        
        logger.info("🛑 Server stopped")

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Reverse Proxy Server")
    parser.add_argument("--client-host", default="0.0.0.0", help="Client WebSocket service host")
    parser.add_argument("--client-port", type=int, default=11435, help="Client WebSocket service port")
    parser.add_argument("--api-host", default="0.0.0.0", help="API service host")
    parser.add_argument("--api-port", type=int, default=11434, help="API service port")
    parser.add_argument("--auth-config", default="auth_config.toml", help="Auth config file (TOML format)")
    
    args = parser.parse_args()
    
    server = ReverseProxyServer(
        client_host=args.client_host,
        client_port=args.client_port,
        api_host=args.api_host,
        api_port=args.api_port,
        auth_config=args.auth_config
    )
    
    try:
        await server.start()
        
        print("\n✅ Server is running. Press Ctrl+C to stop.\n")
        
        # 保持服务器运行
        stop_event = asyncio.Event()
        
        def signal_handler():
            print("\n⚠️  Shutdown signal received...")
            stop_event.set()
        
        # 获取当前事件循环
        loop = asyncio.get_running_loop()
        
        # 注册信号处理器（Unix）
        for sig in [signal.SIGINT, signal.SIGTERM]:
            try:
                loop.add_signal_handler(sig, signal_handler)
            except NotImplementedError:
                # Windows 不支持 add_signal_handler
                pass
        
        # 对于 Windows，使用不同的信号处理
        if sys.platform == 'win32':
            def win_signal_handler():
                signal_handler()
            signal.signal(signal.SIGINT, lambda s, f: win_signal_handler())
            signal.signal(signal.SIGTERM, lambda s, f: win_signal_handler())
        
        # 等待停止信号
        await stop_event.wait()
        
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🛑 Shutting down...")
        await server.stop()
        print("✅ Server stopped")

if __name__ == "__main__":
    import sys
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✅ Exited")