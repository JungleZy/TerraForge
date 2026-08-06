"""
WebSocket event handlers

Handles Socket.IO events for real-time communication between server and clients.
"""

import logging
from flask_socketio import emit

logger = logging.getLogger(__name__)

# Track connected clients.
# 只用于 connect/disconnect 日志里的在线计数,没有其它消费方 —— 保留它
# 是因为排查「事件有没有发给还活着的客户端」时这个计数是唯一线索;
# tests/test_fix_socketio_events.py 依赖它的 add/remove 语义。
connected_clients = set()


def register_socketio_events(socketio):
    """
    Register Socket.IO event handlers

    Args:
        socketio: Flask-SocketIO instance to register events on
    """

    @socketio.on('connect')
    def handle_connect():
        """
        Handle client connection

        Logs connection and tracks client in connected_clients set.
        Emits welcome message to the connected client.
        """
        try:
            from flask import request
            client_id = request.sid
            connected_clients.add(client_id)

            logger.info(f"Client connected: {client_id} (Total: {len(connected_clients)})")

            # Send welcome message to client
            emit('connected', {
                'message': 'Connected to TerraForge',
                'client_id': client_id
            })

            # 底图预热状态快照。必须单独套一层 try:这是附加信息不是连接的前提,
            # 让它把 connect 打挂的话,一个底图相关的小毛病会变成「整个实时推送
            # 用不了」。
            #
            # 为什么 connect 时要推:进度是广播的,中途连上的客户端只会收到之后
            # 的增量。而解压**失败**是终态,事件早就发完了 —— 几小时后才打开
            # 浏览器的用户永远看不到那条失败标记,可「失败要一直显示」是既定要求。
            try:
                from src.services.base_terrain_warmup import EVENT_NAME, snapshot
                emit(EVENT_NAME, snapshot())
            except Exception:
                logger.debug("底图状态快照推送失败(已忽略)", exc_info=True)

        except Exception as e:
            logger.error(f"Error handling client connection: {e}")

    @socketio.on('disconnect')
    def handle_disconnect():
        """
        Handle client disconnection

        Logs disconnection and removes client from connected_clients set.
        """
        try:
            from flask import request
            client_id = request.sid

            if client_id in connected_clients:
                connected_clients.remove(client_id)

            logger.info(f"Client disconnected: {client_id} (Total: {len(connected_clients)})")

        except Exception as e:
            logger.error(f"Error handling client disconnection: {e}")

    logger.debug("Socket.IO events registered")
