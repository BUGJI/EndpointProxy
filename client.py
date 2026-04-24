import asyncio
import json
import logging
import signal
import hashlib
import hmac
from typing import Optional, Dict
import aiohttp
from aiohttp import ClientSession
import argparse
import time
from datetime import datetime
import backoff  # 需要安装: pip install backoff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ReverseProxyClient:
    def __init__(self, 
                 node_id: str, 
                 auth_token: str,
                 server_ws_url: str, 
                 local_server_url: str = "http://127.0.0.1:11434",
                 heartbeat_interval: int = 15,
                 reconnect_delay: int = 5):
        
        self.node_id = node_id
        self.auth_token = auth_token
        self.server_ws_url = server_ws_url
        self.local_server_url = local_server_url
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        
        self.ws = None
        self.session = None
        self.running = True
        self.connected = False
        self.pending_streams: Dict[str, dict] = {}
        
        # 统计
        self.stats = {
            'requests_processed': 0,
            'bytes_transferred': 0,
            'reconnects': 0,
            'last_connected': None
        }
    
    async def create_session(self):
        """创建HTTP会话"""
        timeout = aiohttp.ClientTimeout(total=300, connect=10)
        self.session = ClientSession(timeout=timeout)
    
    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=10,
        max_time=300
    )
    async def connect_with_retry(self):
        """带重连的连接"""
        await self.create_session()
        
        logger.info(f"Connecting to server at {self.server_ws_url} as {self.node_id}")
        
        try:
            self.ws = await self.session.ws_connect(
                self.server_ws_url,
                heartbeat=self.heartbeat_interval * 2,
                autoping=True
            )
            
            # 发送注册信息（包含认证）
            await self.ws.send_json({
                "type": "register",
                "node_id": self.node_id,
                "auth_token": self.auth_token,
                "info": {
                    "local_server_url": self.local_server_url,
                    "version": "2.0.0",
                    "started_at": datetime.now().isoformat()
                }
            })
            
            # 等待注册确认
            msg = await self.ws.receive_json()
            
            if msg.get("type") == "registered":
                self.connected = True
                self.stats['last_connected'] = datetime.now()
                logger.info(f"Successfully registered as {self.node_id}")
                return True
            elif msg.get("type") == "error":
                logger.error(f"Registration failed: {msg.get('message')}")
                return False
            else:
                logger.error(f"Unexpected response: {msg}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self.connected = False
            raise  # 触发重试
    
    async def handle_messages(self):
        """处理消息（支持流式）"""
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self.process_message(data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    logger.info("Server closed connection")
                    self.connected = False
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error")
                    self.connected = False
                    break
        except Exception as e:
            logger.error(f"Message handling error: {e}")
            self.connected = False
    
    async def process_message(self, data: dict):
        """处理服务器消息"""
        msg_type = data.get("type")
        
        if msg_type == "request":
            # 处理请求
            request_id = data.get("request_id")
            method = data.get("method")
            path = data.get("path")
            headers = data.get("headers", {})
            body = data.get("body")
            is_stream = data.get("is_stream", False)
            
            # 根据是否流式选择处理方式
            if is_stream:
                asyncio.create_task(self.handle_stream_request(
                    request_id, method, path, headers, body
                ))
            else:
                asyncio.create_task(self.handle_normal_request(
                    request_id, method, path, headers, body
                ))
        
        elif msg_type == "heartbeat_ack":
            logger.debug("Received heartbeat ACK")
        
        elif msg_type == "error":
            logger.error(f"Server error: {data.get('message')}")
    
    async def handle_normal_request(self, request_id: str, method: str, 
                                    path: str, headers: dict, body: Optional[str]):
        """处理普通HTTP请求"""
        url = f"{self.local_server_url}{path}"
        
        try:
            # 准备请求
            forward_headers = {
                k: v for k, v in headers.items()
                if k.lower() not in ['host', 'content-length', 'connection']
            }
            
            # 发送请求
            async with self.session.request(
                method=method,
                url=url,
                headers=forward_headers,
                data=body.encode('utf-8') if body else None
            ) as response:
                response_body = await response.text()
                
                # 统计
                self.stats['requests_processed'] += 1
                self.stats['bytes_transferred'] += len(response_body)
                
                # 返回响应
                if self.ws and not self.ws.closed:
                    await self.ws.send_json({
                        "type": "response",
                        "request_id": request_id,
                        "data": {
                            "status": response.status,
                            "headers": dict(response.headers),
                            "body": response_body
                        }
                    })
                    logger.debug(f"Completed request {request_id}")
                    
        except Exception as e:
            logger.error(f"Error handling request {request_id}: {e}")
            if self.ws and not self.ws.closed:
                await self.ws.send_json({
                    "type": "response",
                    "request_id": request_id,
                    "data": {
                        "status": 500,
                        "headers": {},
                        "body": json.dumps({"error": str(e)})
                    }
                })
    
    async def handle_stream_request(self, request_id: str, method: str,
                                    path: str, headers: dict, body: Optional[str]):
        """处理流式HTTP请求"""
        url = f"{self.local_server_url}{path}"
        
        try:
            forward_headers = {
                k: v for k, v in headers.items()
                if k.lower() not in ['host', 'content-length', 'connection']
            }
            
            # 发起流式请求
            async with self.session.request(
                method=method,
                url=url,
                headers=forward_headers,
                data=body.encode('utf-8') if body else None
            ) as response:
                # 发送响应头
                await self.ws.send_json({
                    "type": "response_start",
                    "request_id": request_id,
                    "status": response.status,
                    "headers": dict(response.headers)
                })
                
                # 流式传输响应体
                async for chunk in response.content.iter_chunked(8192):
                    if chunk:
                        chunk_str = chunk.decode('utf-8', errors='replace')
                        await self.ws.send_json({
                            "type": "response_chunk",
                            "request_id": request_id,
                            "data": chunk_str
                        })
                        self.stats['bytes_transferred'] += len(chunk_str)
                
                # 发送结束标记
                await self.ws.send_json({
                    "type": "response_end",
                    "request_id": request_id
                })
                
                self.stats['requests_processed'] += 1
                logger.debug(f"Completed stream request {request_id}")
                
        except Exception as e:
            logger.error(f"Error handling stream request {request_id}: {e}")
            if self.ws and not self.ws.closed:
                await self.ws.send_json({
                    "type": "error",
                    "request_id": request_id,
                    "message": str(e)
                })
    
    async def send_heartbeat(self):
        """定期发送心跳"""
        while self.running and self.connected:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                if self.ws and not self.ws.closed:
                    await self.ws.send_json({
                        "type": "heartbeat",
                        "timestamp": time.time()
                    })
                    logger.debug("Heartbeat sent")
            except Exception as e:
                logger.error(f"Failed to send heartbeat: {e}")
                self.connected = False
                break
    
    async def keep_alive(self):
        """保持连接运行，支持自动重连"""
        while self.running:
            try:
                # 尝试连接
                if await self.connect_with_retry():
                    # 启动心跳
                    heartbeat_task = asyncio.create_task(self.send_heartbeat())
                    
                    # 处理消息（阻塞直到断开）
                    await self.handle_messages()
                    
                    # 清理
                    heartbeat_task.cancel()
                    
                    if self.ws and not self.ws.closed:
                        await self.ws.close()
                    if self.session:
                        await self.session.close()
                        self.session = None
                
                # 断开后等待重连
                if self.running:
                    logger.info(f"Reconnecting in {self.reconnect_delay} seconds...")
                    self.stats['reconnects'] += 1
                    await asyncio.sleep(self.reconnect_delay)
                    
            except Exception as e:
                logger.error(f"Connection error: {e}")
                if self.running:
                    await asyncio.sleep(self.reconnect_delay)
    
    async def run(self):
        """运行客户端"""
        await self.keep_alive()
    
    async def stop(self):
        """停止客户端"""
        self.running = False
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session:
            await self.session.close()

async def main():
    parser = argparse.ArgumentParser(description="Reverse Proxy Client")
    parser.add_argument("--node-id", required=True, help="Unique node ID")
    parser.add_argument("--auth-token", required=True, help="Authentication token")
    parser.add_argument("--server-ws", default="ws://127.0.0.1:11435/ws", 
                       help="Server WebSocket URL")
    parser.add_argument("--local-server", default="http://127.0.0.1:11434", 
                       help="Local server URL (Ollama or any HTTP service)")
    parser.add_argument("--heartbeat", type=int, default=15, 
                       help="Heartbeat interval in seconds")
    parser.add_argument("--reconnect-delay", type=int, default=5,
                       help="Reconnect delay in seconds")
    
    args = parser.parse_args()
    
    client = ReverseProxyClient(
        node_id=args.node_id,
        auth_token=args.auth_token,
        server_ws_url=args.server_ws,
        local_server_url=args.local_server,
        heartbeat_interval=args.heartbeat,
        reconnect_delay=args.reconnect_delay
    )
    
    # 优雅关闭
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        asyncio.create_task(client.stop())
    
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    
    await client.run()

if __name__ == "__main__":
    asyncio.run(main())