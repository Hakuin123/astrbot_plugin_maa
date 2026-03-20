import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
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
    "通过 AstrBot 远程控制 MAA",
    "1.0.0",
    "https://github.com/Hakuin123/astrbot_plugin_maa",
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
        self.notify_on_each_task: bool = config.get("notify_on_each_task", False)
        self.custom_address: str = config.get("custom_address", "")

        # 数据存储 (使用 StarTools.get_data_dir 获取规范路径)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_maa")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 设备绑定: {sender_id: {"active_device": str, "devices": {device_id: {"umo": str, "alias": str}}}}
        self.bindings: Dict[str, dict] = {}
        # 反向索引: {device_id: sender_id}
        self.device_to_sender: Dict[str, str] = {}
        # 任务队列: {device_id: [task1, task2, ...]}
        self.task_queues: Dict[str, List[dict]] = {}
        # 已执行的任务 ID: {device_id: set()}
        self.executed_tasks: Dict[str, Set[str]] = {}
        # 设备最后活跃时间: {device_id: timestamp}
        self.device_last_seen: Dict[str, float] = {}
        # 任务信息映射: {task_id: {"name": str, "type": str, "device_id": str}}
        self.task_info: Dict[str, dict] = {}

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
                    data = json.load(f)
                
                migrated = False
                for sender_id, info in data.items():
                    if "device_id" in info:
                        # 旧版格式迁移到新版格式
                        device_id = info["device_id"]
                        self.bindings[sender_id] = {
                            "active_device": device_id,
                            "devices": {
                                device_id: {
                                    "umo": info.get("umo", ""),
                                    "alias": "设备1"
                                }
                            }
                        }
                        migrated = True
                    else:
                        # 新版格式
                        self.bindings[sender_id] = info

                if migrated:
                    self._save_data()

                # 重建反向索引
                for sender_id, user_data in self.bindings.items():
                    for device_id in user_data.get("devices", {}).keys():
                        self.device_to_sender[device_id] = sender_id
                        
                logger.info(f"已加载 {len(self.bindings)} 个用户的设备绑定")
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

    def _get_active_device(self, sender_id: str) -> Optional[str]:
        """获取用户的当前活跃设备 ID"""
        if sender_id in self.bindings:
            user_data = self.bindings[sender_id]
            active_id = user_data.get("active_device")
            if active_id and active_id in user_data.get("devices", {}):
                return active_id
        return None

    @filter.on_astrbot_loaded()
    async def initialize(self):
        """插件初始化，启动 HTTP 服务器"""
        await self._start_http_server()

    async def _start_http_server(self):
        """启动 HTTP 服务器"""
        if self.runner:
            logger.warning("MAA HTTP 服务已在运行中，跳过启动")
            return

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

        # 获取任务信息
        task_info = self.task_info.get(task_id, {})
        task_name = task_info.get("name", "未知任务")
        task_type = task_info.get("type", "")

        # 标记任务已执行
        if device_id not in self.executed_tasks:
            self.executed_tasks[device_id] = set()
        self.executed_tasks[device_id].add(task_id)

        # 从队列移除已完成的任务
        if device_id in self.task_queues:
            self.task_queues[device_id] = [
                t for t in self.task_queues[device_id] if t["id"] != task_id
            ]

        # 清理已完成的任务信息
        if task_id in self.task_info:
            del self.task_info[task_id]

        # 计算剩余用户任务数（排除系统任务如截图、心跳等）
        system_task_types = {"CaptureImage", "CaptureImageNow", "HeartBeat", "StopTask"}
        remaining_user_tasks = len([
            t for t in self.task_queues.get(device_id, [])
            if t.get("type") not in system_task_types
        ])

        # 判断是否应该发送通知
        is_system_task = task_type in system_task_types
        # 非系统任务时：每任务通知模式直接通知，否则仅当所有任务完成才通知
        should_notify = not is_system_task and (
            self.notify_on_each_task or remaining_user_tasks == 0
        )

        # 查找对应用户并发送通知
        sender_id = self.device_to_sender.get(device_id)
        if sender_id and sender_id in self.bindings:
            user_data = self.bindings[sender_id]
            device_info = user_data.get("devices", {}).get(device_id, {})
            alias = device_info.get("alias", device_id[:8])
            
            if umo := device_info.get("umo"):
                # 构建通知消息
                if should_notify:
                    if self.notify_on_each_task:
                        message = f"✅ MAA 任务完成 [{alias}]：{task_name}\n状态: {status}"
                        if remaining_user_tasks > 0:
                            message += f"\n剩余任务: {remaining_user_tasks} 个"
                    else:
                        message = f"✅ MAA 所有任务已完成 [{alias}]\n最后完成: {task_name}\n状态: {status}"

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
                elif payload and len(payload) > 100:
                    # 截图任务：仅发送截图，不发送通知文本
                    try:
                        await self._send_screenshot(umo, payload, "")
                    except Exception as e:
                        logger.error(f"发送截图失败: {e}")

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
        chain = MessageChain().message(message) if message else MessageChain()
        chain = chain.file_image(str(temp_file))
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

    def _add_task(self, device_id: str, task_type: str, task_name: str = "", params: str = "") -> str:
        """添加任务到队列，返回任务 ID
        
        Args:
            device_id: 设备 ID
            task_type: 任务类型
            task_name: 任务名称（用于通知显示）
            params: 任务参数
        """
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": task_type}
        if params:
            task["params"] = params

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []

        self.task_queues[device_id].append(task)

        # 存储任务信息以便完成时获取任务名
        self.task_info[task_id] = {
            "name": task_name or task_type,
            "type": task_type,
            "device_id": device_id
        }

        # 如果开启自动截图，追加截图任务
        if self.auto_screenshot and task_type not in ("CaptureImage", "CaptureImageNow", "HeartBeat"):
            screenshot_task_id = str(uuid.uuid4())
            screenshot_task = {"id": screenshot_task_id, "type": "CaptureImage"}
            self.task_queues[device_id].append(screenshot_task)
            self.task_info[screenshot_task_id] = {
                "name": "自动截图",
                "type": "CaptureImage",
                "device_id": device_id
            }

        return task_id

    # ==================== 指令处理 ====================

    @filter.command_group("maa")
    def maa(self):
        """MAA 远程控制指令组"""
        pass

    @maa.command("bind")
    async def maa_bind(self, event: AstrMessageEvent, device_id: str, alias: str = ""):
        """绑定 MAA 设备

        用法: /maa bind <设备标识符> [设备别名]
        设备标识符可在 MAA 设置中查看
        """
        sender_id = event.get_sender_id()

        # 检查设备是否已被其他用户绑定
        if device_id in self.device_to_sender and self.device_to_sender[device_id] != sender_id:
            yield event.plain_result("❌ 错误：该设备已被其他用户绑定")
            return

        user_data = self.bindings.setdefault(sender_id, {"active_device": "", "devices": {}})
        
        if device_id in user_data["devices"]:
            # 如果已存在，允许更新别名
            if alias:
                user_data["devices"][device_id]["alias"] = alias
                self._save_data()
                yield event.plain_result(f"✅ 设备别名已更新为: {alias}")
            else:
                yield event.plain_result(f"⚠︎ 该设备已绑定。")
            return

        if not alias:
            alias = f"设备{len(user_data['devices']) + 1}"

        # 保存绑定信息
        user_data["devices"][device_id] = {
            "umo": event.unified_msg_origin,
            "alias": alias
        }
        
        # 如果是第一个设备，或者未设置活跃设备，将其设为活跃设备
        if not user_data["active_device"]:
            user_data["active_device"] = device_id
            
        self.device_to_sender[device_id] = sender_id
        self._save_data()

        base_url = self.custom_address if self.custom_address else f"<你的地址>:{self.http_port}"

        yield event.plain_result(
            f"✅ 绑定成功！\n\n"
            f"设备别名: {alias}\n"
            f"设备ID: {device_id[:16]}...\n\n"
            f"请在 MAA 设置-远程控制 配置以下端点:\n"
            f"• 获取任务: {base_url}/maa/getTask\n"
            f"• 汇报状态: {base_url}/maa/reportStatus\n"
            f"• 用户标识符: {sender_id}"
        )

    @maa.command("unbind")
    async def maa_unbind(self, event: AstrMessageEvent, identifier: str = ""):
        """解绑 MAA 设备
        
        用法: /maa unbind [设备ID或别名]
        """
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings or not self.bindings[sender_id]["devices"]:
            yield event.plain_result("❌ 错误：尚未绑定任何设备")
            return

        user_data = self.bindings[sender_id]
        devices = user_data["devices"]

        target_device_id = None
        if not identifier:
            if len(devices) == 1:
                target_device_id = list(devices.keys())[0]
            else:
                yield event.plain_result("⚠︎ 当前已绑定多个设备，请指定要解绑的设备ID或别名。\n查看设备列表请使用: /maa list")
                return
        else:
            # 尝试通过 ID 或别名匹配
            for d_id, info in devices.items():
                if d_id.startswith(identifier) or info.get("alias") == identifier:
                    target_device_id = d_id
                    break

        if not target_device_id:
            yield event.plain_result(f"❌ 未找到匹配的设备: {identifier}")
            return

        alias = devices[target_device_id].get("alias", "")
        
        # 清理数据
        del self.device_to_sender[target_device_id]
        del devices[target_device_id]
        
        if target_device_id in self.task_queues:
            del self.task_queues[target_device_id]
        if target_device_id in self.executed_tasks:
            del self.executed_tasks[target_device_id]

        # 如果解绑的是当前活跃设备，自动切换到其他设备（如果有）
        if user_data["active_device"] == target_device_id:
            if devices:
                user_data["active_device"] = list(devices.keys())[0]
            else:
                user_data["active_device"] = ""

        # 如果所有设备都解绑了，可以清理 user_data
        if not devices:
            del self.bindings[sender_id]

        self._save_data()

        msg = f"✅ 已解绑设备: {alias} ({target_device_id[:8]}...)"
        if sender_id in self.bindings and self.bindings[sender_id]["active_device"]:
            new_active = self.bindings[sender_id]['active_device']
            new_alias = self.bindings[sender_id]['devices'][new_active].get('alias', '')
            msg += f"\n当前活跃设备已切换为: {new_alias} ({new_active[:8]}...)"
            
        yield event.plain_result(msg)

    @maa.command("list")
    async def maa_list(self, event: AstrMessageEvent):
        """列出所有绑定的设备"""
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings or not self.bindings[sender_id]["devices"]:
            yield event.plain_result("ℹ️ 尚未绑定任何设备")
            return

        user_data = self.bindings[sender_id]
        devices = user_data["devices"]
        active_device = user_data["active_device"]

        lines = ["📱 已绑定的 MAA 设备列表："]
        for d_id, info in devices.items():
            alias = info.get("alias", "")
            
            # 获取状态
            last_seen = self.device_last_seen.get(d_id, 0)
            now = time.time()
            if last_seen > 0:
                elapsed = now - last_seen
                if elapsed < 10:
                    status = "🟢在线"
                elif elapsed < 60:
                    status = "🟡闲置"
                else:
                    status = "🔴离线"
            else:
                status = "⚪未连"

            marker = "👉 " if d_id == active_device else "   "
            lines.append(f"{marker}[{alias}] {status} - {d_id[:8]}...")

        lines.append("\n提示：使用 /maa switch <别名/ID> 切换当前控制的设备")
        yield event.plain_result("\n".join(lines))

    @maa.command("rename")
    async def maa_rename(self, event: AstrMessageEvent, old_identifier: str, new_alias: str):
        """重命名设备别名
        
        用法: /maa rename <设备ID或旧别名> <新别名>
        """
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings or not self.bindings[sender_id]["devices"]:
            yield event.plain_result("❌ 错误：尚未绑定任何设备")
            return

        user_data = self.bindings[sender_id]
        devices = user_data["devices"]

        target_device_id = None
        for d_id, info in devices.items():
            if d_id.startswith(old_identifier) or info.get("alias") == old_identifier:
                target_device_id = d_id
                break

        if not target_device_id:
            yield event.plain_result(f"❌ 未找到匹配的设备: {old_identifier}\n使用 /maa list 查看设备列表")
            return
            
        old_alias = devices[target_device_id].get("alias", "")
        devices[target_device_id]["alias"] = new_alias
        self._save_data()
        
        yield event.plain_result(f"✅ 设备已重命名: {old_alias} -> {new_alias}")

    @maa.command("switch", alias={"use"})
    async def maa_switch(self, event: AstrMessageEvent, identifier: str):
        """切换当前活跃的设备
        
        用法: /maa switch <设备ID或别名>
        """
        sender_id = event.get_sender_id()

        if sender_id not in self.bindings or not self.bindings[sender_id]["devices"]:
            yield event.plain_result("❌ 错误：尚未绑定任何设备")
            return

        user_data = self.bindings[sender_id]
        devices = user_data["devices"]

        target_device_id = None
        for d_id, info in devices.items():
            if d_id.startswith(identifier) or info.get("alias") == identifier:
                target_device_id = d_id
                break

        if not target_device_id:
            yield event.plain_result(f"❌ 未找到匹配的设备: {identifier}\n使用 /maa list 查看设备列表")
            return

        user_data["active_device"] = target_device_id
        self._save_data()
        
        alias = devices[target_device_id].get("alias", "")
        yield event.plain_result(f"✅ 已切换到设备: {alias} ({target_device_id[:8]}...)")

    @maa.command("status")
    async def maa_status(self, event: AstrMessageEvent):
        """查看当前设备状态"""
        sender_id = event.get_sender_id()
        device_id = self._get_active_device(sender_id)

        if not device_id:
            yield event.plain_result("❌ 错误：尚未绑定设备或未设置活跃设备\n使用 /maa bind <设备ID> 绑定")
            return

        user_data = self.bindings[sender_id]
        alias = user_data["devices"][device_id].get("alias", "")

        # 检查设备在线状态
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
            f"📊 MAA 设备状态 [{alias}]\n\n"
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
        device_id = self._get_active_device(sender_id)

        if not device_id:
            yield event.plain_result("❌ 错误：请先绑定设备: /maa bind <设备ID>")
            return

        alias = self.bindings[sender_id]["devices"][device_id].get("alias", "")

        # 解析任务列表（英文逗号分隔）
        task_names = [t.strip() for t in tasks.split(",") if t.strip()]
        if not task_names:
            yield event.plain_result("❌ 错误：请指定要执行任务\n用法: /maa start ALL 或 /maa start 刷理智,公招")
            return

        # 解析任务类型
        task_types = []
        for name in task_names:
            # 查找映射（键值不区分大小写，中文精确匹配）
            task_type = TASK_ALIASES.get(name.lower()) or TASK_ALIASES.get(name)
            if not task_type:
                yield event.plain_result(
                    f"❌ 错误：未知任务: {name}\n\n"
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
            task_id = self._add_task(device_id, task_type, task_name=name)
            added_tasks.append(f"• {name} ({task_type})")

        yield event.plain_result(
            f"✅ 已添加 {len(added_tasks)} 个任务到设备 [{alias}]\n\n"
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
        device_id = self._get_active_device(sender_id)

        if not device_id:
            yield event.plain_result("❌ 错误：请先绑定设备: /maa bind <设备ID>")
            return

        alias = self.bindings[sender_id]["devices"][device_id].get("alias", "")
        
        # 使用立即截图任务，不等待队列
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "CaptureImageNow"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)  # 插入队首

        yield event.plain_result(f"📸 截图任务已添加到设备 [{alias}]，等待 MAA 响应")

    @maa.command("stop")
    async def maa_stop(self, event: AstrMessageEvent):
        """停止当前任务"""
        sender_id = event.get_sender_id()
        device_id = self._get_active_device(sender_id)

        if not device_id:
            yield event.plain_result("❌ 错误：请先绑定设备: /maa bind <设备ID>")
            return

        alias = self.bindings[sender_id]["devices"][device_id].get("alias", "")
        
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "StopTask"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)

        yield event.plain_result(f"🛑 停止任务指令已发送到设备 [{alias}]")

    @maa.command("heartbeat", alias={"ping"})
    async def maa_heartbeat(self, event: AstrMessageEvent):
        """发送心跳检测"""
        sender_id = event.get_sender_id()
        device_id = self._get_active_device(sender_id)

        if not device_id:
            yield event.plain_result("❌ 错误：请先绑定设备: /maa bind <设备ID>")
            return

        alias = self.bindings[sender_id]["devices"][device_id].get("alias", "")
        
        task_id = str(uuid.uuid4())
        task = {"id": task_id, "type": "HeartBeat"}

        if device_id not in self.task_queues:
            self.task_queues[device_id] = []
        self.task_queues[device_id].insert(0, task)

        yield event.plain_result(f"💓 心跳检测已发送到设备 [{alias}]，等待 MAA 返回当前任务状态")

    async def terminate(self):
        """插件销毁，停止 HTTP 服务器"""
        logger.info(f"正在停止 MAA HTTP 服务 (端口: {self.http_port})...")
        try:
            # 使用 asyncio.wait_for 以确保停止操作不会永久挂起
            async def perform_cleanup():
                if self.site:
                    await self.site.stop()
                    logger.debug("MAA HTTP Site 已停止")
                if self.runner:
                    await self.runner.cleanup()
                    logger.debug("MAA HTTP Runner 已清理")
                if self.app:
                    await self.app.shutdown()
                    await self.app.cleanup()
                    logger.debug("MAA HTTP App 已关闭")

            await asyncio.wait_for(perform_cleanup(), timeout=10.0)
            logger.info("MAA HTTP 服务停止成功")
        except asyncio.TimeoutError:
            logger.error("停止 MAA HTTP 服务超时")
        except Exception as e:
            logger.error(f"停止 MAA HTTP 服务时发生错误: {e}")
        finally:
            self.site = None
            self.runner = None
            self.app = None
            logger.info("MAA 插件已销毁")
