"""
MAA 远程控制 AstrBot 插件
通过消息平台远程控制 MAA
"""

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp


# 任务别名映射表
TASK_ALIASES = {
    # 键值 (不区分大小写)
    "base": "LinkStart-Base",
    "wakeup": "LinkStart-WakeUp",
    "combat": "LinkStart-Combat",
    "recruiting": "LinkStart-Recruiting",
    "mall": "LinkStart-Mall",
    "mission": "LinkStart-Mission",
    "autoroguelike": "LinkStart-AutoRoguelike",
    "reclamation": "LinkStart-Reclamation",
    # 中文别名
    "基建换班": "LinkStart-Base",
    "基建": "LinkStart-Base",
    "开始唤醒": "LinkStart-WakeUp",
    "刷理智": "LinkStart-Combat",
    "自动公招": "LinkStart-Recruiting",
    "公招": "LinkStart-Recruiting",
    "获取信用及购物": "LinkStart-Mall",
    "信用": "LinkStart-Mall",
    "领取奖励": "LinkStart-Mission",
    "自动肉鸽": "LinkStart-AutoRoguelike",
    "肉鸽": "LinkStart-AutoRoguelike",
    "生息演算": "LinkStart-Reclamation",
    # 特殊值
    "all": "LinkStart",
}


@register(
    "astrbot_plugin_maa",
    "Hakuin123",
    "通过消息平台远程控制 MAA",
    "1.0.0",
    "https://github.com/Hakuin123/astrbot_plugin_MAA",
)
class MAAPlugin(Star):
    """MAA 远程控制插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # HTTP 服务配置
        self.http_host: str = config.get("http_host", "0.0.0.0")
        self.http_port: int = config.get("http_port", 2828)
        self.auto_screenshot: bool = config.get("auto_screenshot", True)

        # 数据存储
        self.data_dir = Path("data/astrbot_plugin_maa")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 设备绑定: {sender_id: {"device_id": str, "user_id": str, "umo": str}}
        self.bindings: Dict[str, dict] = {}
        # 反向索引: {device_id: sender_id}
        self.device_to_sender: Dict[str, str] = {}
        # 任务队列: {device_id: [task1, task2, ...]}
        self.task_queues: Dict[str, List[dict]] = {}
        # 已执行的任务 ID: {device_id: set()}
        self.executed_tasks: Dict[str, Set[str]] = {}
        # 设备最后活跃时间: {device_id: timestamp}
        self.device_last_seen: Dict[str, float] = {}

        # HTTP 服务器相关
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # 加载持久化数据
        self._load_data()

    def _load_data(self):
        """从文件加载持久化数据"""
        bindings_file = self.data_dir / "bindings.json"
        if bindings_file.exists():
            try:
                with open(bindings_file, "r", encoding="utf-8") as f:
                    self.bindings = json.load(f)
                # 重建反向索引
                for sender_id, info in self.bindings.items():
                    self.device_to_sender[info["device_id"]] = sender_id
                logger.info(f"已加载 {len(self.bindings)} 个设备绑定")
            except Exception as e:
                logger.error(f"加载绑定数据失败: {e}")

    def _save_data(self):
        """保存持久化数据到文件"""
        bindings_file = self.data_dir / "bindings.json"
        try:
            with open(bindings_file, "w", encoding="utf-8") as f:
                json.dump(self.bindings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存绑定数据失败: {e}")

    async def initialize(self):
        """插件初始化，启动 HTTP 服务器"""
        await self._start_http_server()

    async def _start_http_server(self):
        """启动 HTTP 服务器"""
        self.app = web.Application()
        self.app.router.add_post("/maa/getTask", self._handle_get_task)
        self.app.router.add_post("/maa/reportStatus", self._handle_report_status)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        try:
            self.site = web.TCPSite(self.runner, self.http_host, self.http_port)
            await self.site.start()
            logger.info(f"MAA HTTP 服务已启动: http://{self.http_host}:{self.http_port}")
        except OSError as e:
            logger.error(f"HTTP 服务启动失败，端口 {self.http_port} 可能被占用: {e}")

    async def _handle_get_task(self, request: web.Request) -> web.Response:
        """处理 MAA 获取任务请求"""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"tasks": []}, status=400)

        device_id = data.get("device", "")
        user_id = data.get("user", "")

        if not device_id:
            return web.json_response({"tasks": []}, status=400)

        # 更新设备最后活跃时间
        import time
        self.device_last_seen[device_id] = time.time()

        # 检查是否是已绑定的设备
        sender_id = self.device_to_sender.get(device_id)
        if not sender_id:
            # 设备未绑定，返回空任务但记录日志
            logger.debug(f"未绑定设备请求: device={device_id}, user={user_id}")
            return web.json_response({"tasks": []})

        # 获取任务队列
        tasks = self.task_queues.get(device_id, [])
        executed = self.executed_tasks.get(device_id, set())

        # 过滤已执行的任务
        pending_tasks = [t for t in tasks if t["id"] not in executed]

        return web.json_response({"tasks": pending_tasks})

    async def _handle_report_status(self, request: web.Request) -> web.Response:
        """处理 MAA 汇报任务状态"""
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400)

        device_id = data.get("device", "")
        task_id = data.get("task", "")
        status = data.get("status", "")
        payload = data.get("payload", "")

        if not device_id or not task_id:
            return web.Response(status=400)

        # 标记任务已执行
        if device_id not in self.executed_tasks:
            self.executed_tasks[device_id] = set()
        self.executed_tasks[device_id].add(task_id)

        # 从队列移除已完成的任务
        if device_id in self.task_queues:
            self.task_queues[device_id] = [
                t for t in self.task_queues[device_id] if t["id"] != task_id
            ]

        # 查找对应用户并发送通知
        sender_id = self.device_to_sender.get(device_id)
        if sender_id and sender_id in self.bindings:
            binding = self.bindings[sender_id]
            if umo := binding.get("umo"):
                # 发送任务完成通知
                message = f"✅ MAA 任务完成\n状态: {status}"

                # 如果有截图数据（Base64），发送图片
                if payload and len(payload) > 100:  # 可能是截图
                    try:
                        await self._send_screenshot(umo, payload, message)
                    except Exception as e:
                        logger.error(f"发送截图失败: {e}")
                        chain = MessageChain().message(f"{message}\n(截图发送失败: {e})")
                        await self.context.send_message(umo, chain)
                else:
                    chain = MessageChain().message(message)
                    await self.context.send_message(umo, chain)

        return web.Response(status=200)

    async def _send_screenshot(self, umo: str, base64_data: str, message: str):
        """解码并发送截图"""
        # 解码 Base64
        image_data = base64.b64decode(base64_data)

        # 保存为临时文件
        temp_dir = self.data_dir / "temp"
        temp_dir.mkdir(exist_ok=True)
        temp_file = temp_dir / f"screenshot_{uuid.uuid4().hex[:8]}.png"

        with open(temp_file, "wb") as f:
            f.write(image_data)

        # 发送消息和图片
        chain = MessageChain().message(message).file_image(str(temp_file))
        await self.context.send_message(umo, chain)

        # 延迟删除临时文件
        asyncio.create_task(self._delete_temp_file(temp_file))

    async def _delete_temp_file(self, file_path: Path, delay: float = 30.0):
        """延迟删除临时文件"""
        await asyncio.sleep(delay)
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            logger.debug(f"删除临时文件失败: {e}")

    def _add_task(self, device_id: str, task_type: str, params: str = "") -> str:
        """添加任务到队列，返回任务 ID"""
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": task_type}
        if params:
            task["params"] = params

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []

        self.task_queues[device_id].append(task)

        # 如果开启自动截图，追加截图任务
        if self.auto_screenshot and task_type not in ("CaptureImage", "CaptureImageNow", "HeartBeat"):
            screenshot_task = {"id": str(uuid.uuid4()), "type": "CaptureImage"}
            self.task_queues[device_id].append(screenshot_task)

        return task_id

    # ==================== 指令处理 ====================

    @filter.command_group("maa")
    def maa(self):
        """MAA 远程控制指令组"""
        pass

    @maa.command("bind")
    async def maa_bind(self, event: AstrMessageEvent, device_id: str):
        """绑定 MAA 设备

        用法: /maa bind <设备标识符>
        设备标识符可在 MAA 设置中查看
        """
        sender_id = event.get_sender_id()

        # 检查是否已绑定其他设备
        if sender_id in self.bindings:
            old_device = self.bindings[sender_id]["device_id"]
            yield event.plain_result(
                f"⚠️ 你已绑定设备: {old_device[:8]}...\n"
                "请先使用 /maa unbind 解绑后再绑定新设备"
            )
            return

        # 检查设备是否已被其他用户绑定
        if device_id in self.device_to_sender:
            yield event.plain_result("❌ 该设备已被其他用户绑定")
            return

        # 保存绑定信息
        self.bindings[sender_id] = {
            "device_id": device_id,
            "user_id": sender_id,  # 可作为 MAA 的用户标识符
            "umo": event.unified_msg_origin,
        }
        self.device_to_sender[device_id] = sender_id
        self._save_data()

        yield event.plain_result(
            f"✅ 绑定成功！\n\n"
            f"🖥️ 设备ID: {device_id[:16]}...\n\n"
            f"请在 MAA 中配置以下端点:\n"
            f"• 获取任务: http://<你的IP>:{self.http_port}/maa/getTask\n"
            f"• 汇报状态: http://<你的IP>:{self.http_port}/maa/reportStatus\n"
            f"• 用户标识符: {sender_id}"
        )

    @maa.command("unbind")
    async def maa_unbind(self, event: AstrMessageEvent):
        """解绑 MAA 设备"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 你尚未绑定任何设备")
            return

        old_device = self.bindings[sender_id]["device_id"]

        # 清理数据
        del self.device_to_sender[old_device]
        del self.bindings[sender_id]
        if old_device in self.task_queues:
            del self.task_queues[old_device]
        if old_device in self.executed_tasks:
            del self.executed_tasks[old_device]

        self._save_data()

        yield event.plain_result(f"✅ 已解绑设备: {old_device[:16]}...")

    @maa.command("status")
    async def maa_status(self, event: AstrMessageEvent):
        """查看设备状态"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 你尚未绑定任何设备\n使用 /maa bind <设备ID> 绑定")
            return

        binding = self.bindings[sender_id]
        device_id = binding["device_id"]

        # 检查设备在线状态
        import time
        last_seen = self.device_last_seen.get(device_id, 0)
        now = time.time()
        if last_seen > 0:
            elapsed = now - last_seen
            if elapsed < 10:
                status = "🟢 在线"
            elif elapsed < 60:
                status = f"🟡 {int(elapsed)}秒前活跃"
            else:
                status = f"🔴 离线 ({int(elapsed // 60)}分钟前)"
        else:
            status = "⚪ 从未连接"

        # 任务队列状态
        pending = len(self.task_queues.get(device_id, []))

        yield event.plain_result(
            f"📊 MAA 设备状态\n\n"
            f"设备ID: {device_id[:16]}...\n"
            f"状态: {status}\n"
            f"待执行任务: {pending} 个"
        )

    @maa.command("start")
    async def maa_start(self, event: AstrMessageEvent, tasks: str):
        """执行指定任务

        用法:
          /maa start ALL                    - 完整一键长草
          /maa start 自动肉鸽               - 单个任务
          /maa start 开始唤醒,刷理智,信用   - 多个任务（英文逗号分隔）

        可用任务:
          Base/基建换班/基建, WakeUp/开始唤醒, Combat/刷理智,
          Recruiting/自动公招/公招, Mall/获取信用及购物/信用,
          Mission/领取奖励, AutoRoguelike/自动肉鸽/肉鸽, Reclamation/生息演算
        """
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 请先绑定设备: /maa bind <设备ID>")
            return

        device_id = self.bindings[sender_id]["device_id"]

        # 解析任务列表（英文逗号分隔）
        task_names = [t.strip() for t in tasks.split(",") if t.strip()]
        if not task_names:
            yield event.plain_result("❌ 请指定要执行的任务\n用法: /maa start ALL 或 /maa start 刷理智,公招")
            return

        # 解析任务类型
        task_types = []
        for name in task_names:
            # 查找映射（键值不区分大小写，中文精确匹配）
            task_type = TASK_ALIASES.get(name.lower()) or TASK_ALIASES.get(name)
            if not task_type:
                yield event.plain_result(
                    f"❌ 未知任务: {name}\n\n"
                    f"可用任务:\n"
                    f"  ALL - 完整一键长草\n"
                    f"  Base/基建换班/基建\n"
                    f"  WakeUp/开始唤醒\n"
                    f"  Combat/刷理智\n"
                    f"  Recruiting/自动公招/公招\n"
                    f"  Mall/获取信用及购物/信用\n"
                    f"  Mission/领取奖励\n"
                    f"  AutoRoguelike/自动肉鸽/肉鸽\n"
                    f"  Reclamation/生息演算"
                )
                return
            task_types.append((name, task_type))

        # 添加任务到队列
        added_tasks = []
        for name, task_type in task_types:
            task_id = self._add_task(device_id, task_type)
            added_tasks.append(f"• {name} ({task_type})")

        yield event.plain_result(
            f"✅ 已添加 {len(added_tasks)} 个任务\n\n"
            + "\n".join(added_tasks) + "\n\n"
            f"MAA 将在下次轮询时执行"
        )

    @maa.command("linkstart")
    async def maa_linkstart(self, event: AstrMessageEvent):
        """执行完整一键长草任务 (快捷方式)"""
        async for res in self.maa_start(event, "ALL"):
            yield res

    @maa.command("screenshot", alias={"cap", "ss"})
    async def maa_screenshot(self, event: AstrMessageEvent):
        """获取当前截图"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 请先绑定设备: /maa bind <设备ID>")
            return

        device_id = self.bindings[sender_id]["device_id"]
        # 使用立即截图任务，不等待队列
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "CaptureImageNow"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)  # 插入队首

        yield event.plain_result("📸 截图任务已添加，稍后将收到截图")

    @maa.command("stop")
    async def maa_stop(self, event: AstrMessageEvent):
        """停止当前任务"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 请先绑定设备: /maa bind <设备ID>")
            return

        device_id = self.bindings[sender_id]["device_id"]
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "StopTask"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)

        yield event.plain_result("🛑 停止任务指令已发送")

    @maa.command("heartbeat", alias={"ping"})
    async def maa_heartbeat(self, event: AstrMessageEvent):
        """发送心跳检测"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings:
            yield event.plain_result("❌ 请先绑定设备: /maa bind <设备ID>")
            return

        device_id = self.bindings[sender_id]["device_id"]
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "HeartBeat"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)

        yield event.plain_result("💓 心跳检测已发送，稍后将返回当前任务状态")

    async def terminate(self):
        """插件销毁，停止 HTTP 服务器"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("MAA HTTP 服务已停止")
