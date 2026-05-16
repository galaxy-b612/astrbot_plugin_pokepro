import copy
import time
import random
import asyncio
import psutil
from dataclasses import dataclass, field
from typing import Optional, List

from astrbot.api import logger
from astrbot.api.message_components import Face
from astrbot.core.message.components import At, Plain, Record, Image as CompImage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.context import Context

from .config import PluginConfig
from .cooldown import Cooldown
from .llm import LLMService
from .model import PokeEvent, PokeModel
from .send_poke import PokeSender
from .utils import get_nickname
from .poke_ban_manager import poke_ban_manager


def get_klee_style_name(name: str) -> str:
    """将昵称转换为可莉风格的称呼"""
    if not name:
        return "旅行者"
    
    name = name.strip()
    
    if len(name) > 5:
        name = name[:5]
    
    suffixes = ["姐姐", "哥哥", "旅行者"]
    return name + random.choice(suffixes)


@dataclass
class CachedMemeTemplates:
    """缓存的表情包模板分类"""
    avatar_templates: list = field(default_factory=list)
    text_templates: list = field(default_factory=list)
    last_refresh: float = 0.0
    refresh_interval: float = 3600.0


@dataclass
class AvatarPreCache:
    """头像预缓存（系统内存感知版）"""
    cache: dict = field(default_factory=dict)
    access_count: dict = field(default_factory=dict)
    ttl: dict = field(default_factory=dict)
    
    max_size: int = 20
    max_memory_mb: float = 2.0
    current_memory: float = 0.0
    last_cleanup: float = 0.0
    
    cleanup_interval: float = 60.0
    ttl_seconds: float = 180.0
    
    memory_pressure_threshold: float = 300.0
    aggressive_threshold: float = 250.0
    
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    def _get_available_memory_mb(self) -> float:
        """获取系统可用内存（MB）"""
        try:
            return psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return 999999.0
    
    def _should_cleanup(self, available_mb: float) -> tuple:
        """判断是否需要清理，返回 (是否清理, 策略名称, 移除数量)"""
        now = time.time()
        
        # 策略1：内存严重紧张 - 全部清理
        if available_mb < self.aggressive_threshold:
            return True, "aggressive", len(self.cache)
        
        # 策略2：内存紧张 - 清理一半
        if available_mb < self.memory_pressure_threshold:
            return True, "partial", len(self.cache) // 2
        
        # 策略3：内存超限 - 清理到限制
        if self.current_memory > self.max_memory_mb * 1024 * 1024:
            excess = len(self.cache) - self.max_size
            if excess > 0:
                return True, "lru", excess
        
        # 策略4：过期清理
        expired = [
            uid for uid, expire_at in self.ttl.items()
            if now > expire_at
        ]
        if expired:
            return True, "expired", len(expired)
        
        # 策略5：定时 LRU
        if now - self.last_cleanup >= self.cleanup_interval:
            excess = len(self.cache) - self.max_size
            if excess > 0:
                return True, "lru", excess
        
        return False, "", 0
    
    async def cleanup_check(self):
        """检查并执行清理"""
        available_mb = self._get_available_memory_mb()
        should_clean, strategy, count = self._should_cleanup(available_mb)
        
        if not should_clean:
            return
        
        async with self._lock:
            await self._execute_cleanup(strategy, count)
    
    async def _execute_cleanup(self, strategy: str, count: int):
        """执行清理"""
        if strategy == "aggressive":
            released = self.current_memory
            self.cache.clear()
            self.access_count.clear()
            self.ttl.clear()
            self.current_memory = 0.0
            logger.warning(
                f"[戳一戳] 内存紧张({self._get_available_memory_mb():.0f}MB)，"
                f"激进清理头像缓存，释放 {released/1024/1024:.2f}MB"
            )
            return
        
        # 获取清理目标
        if strategy == "expired":
            now = time.time()
            targets = [uid for uid, expire_at in self.ttl.items() if now > expire_at]
        else:
            sorted_users = sorted(
                self.access_count.items(),
                key=lambda x: x[1]
            )
            targets = [uid for uid, _ in sorted_users[:count]]
        
        # 执行删除
        released = 0.0
        for uid in targets:
            if uid in self.cache:
                released += len(self.cache[uid])
                del self.cache[uid]
            self.access_count.pop(uid, None)
            self.ttl.pop(uid, None)
        
        self.current_memory -= released
        self.last_cleanup = time.time()
        
        if released > 0:
            logger.info(
                f"[戳一戳] {strategy}清理头像缓存，"
                f"移除 {len(targets)} 个，释放 {released/1024/1024:.2f}MB"
            )
    
    def add(self, user_id: str, avatar: bytes) -> bool:
        """添加头像到缓存"""
        size = len(avatar)
        
        if len(self.cache) >= self.max_size:
            return False
        if self.current_memory + size > self.max_memory_mb * 1024 * 1024:
            return False
        
        self.cache[user_id] = avatar
        self.access_count[user_id] = 1
        self.ttl[user_id] = time.time() + self.ttl_seconds
        self.current_memory += size
        return True
    
    def get(self, user_id: str) -> Optional[bytes]:
        """获取缓存的头像"""
        if user_id not in self.cache:
            return None
        
        if time.time() > self.ttl.get(user_id, 0):
            return None
        
        self.access_count[user_id] = self.access_count.get(user_id, 0) + 1
        return self.cache[user_id]
    
    def __len__(self) -> int:
        return len(self.cache)


class GetPokeHandler:
    def __init__(self, context: Context, config: PluginConfig, poke_sender: PokeSender):
        self.context = context
        self.cfg = config
        self.sender = poke_sender
        self.llm = LLMService(context, self.cfg)
        self.cooldown = Cooldown(self.cfg)
        
        # 连续戳计数
        self._poke_count: dict[str, int] = {}
        self._last_poke_time: dict[str, float] = {}
        self._poke_threshold = 5
        self._poke_ban_duration = 600
        self._poke_reset_interval = 30

        # Meme Generator 相关
        self._meme_templates = CachedMemeTemplates()
        self._avatar_cache = AvatarPreCache(
            max_size=config.meme.cache_max_users,
            max_memory_mb=config.meme.cache_max_memory_mb,
            ttl_seconds=config.meme.cache_ttl_seconds,
            memory_pressure_threshold=config.meme.cache_memory_pressure_mb,
            aggressive_threshold=config.meme.cache_aggressive_threshold_mb,
        )
        self._meme_cooldown: dict[str, float] = {}
        self._meme_manager = None
        self._meme_manager_checked = False

        self.handlers = {
            PokeModel.ANTIPOKE: self.respond_poke,
            PokeModel.LLM: self.respond_llm,
            PokeModel.FACE: self.respond_face,
            PokeModel.MEME: self.respond_meme,
            PokeModel.RECORD: self.respond_record,
            PokeModel.BAN: self.respond_ban,
            PokeModel.COMMAND: self.respond_cmd,
        }

        self._modules, self._weights = self._build_response_pool()
        
        # 异步初始化模板缓存
        asyncio.create_task(self._init_meme_templates())

    def _build_response_pool(self):
        pool = []
        for module, handler in self.handlers.items():
            weight = self.cfg.weight_of(module)
            if weight > 0:
                pool.append((module, weight))

        if not pool:
            logger.warning("所有响应模块权重均为 0，戳一戳功能已禁用")
            return (), ()

        modules, weights = zip(*pool)
        return modules, weights

    def _get_meme_manager(self):
        """获取 MemeManager 实例（使用正确的 AstrBot API）"""
        if self._meme_manager is not None:
            return self._meme_manager
        
        if self._meme_manager_checked:
            return None
        
        self._meme_manager_checked = True
        
        try:
            # 方案1: 通过 get_registered_star (AstrBot 标准 API)
            if hasattr(self.context, 'get_registered_star'):
                try:
                    star_meta = self.context.get_registered_star("astrbot_plugin_meme_generator")
                    if star_meta and hasattr(star_meta, 'star_cls') and star_meta.star_cls:
                        if hasattr(star_meta.star_cls, 'meme_manager'):
                            self._meme_manager = star_meta.star_cls.meme_manager
                            logger.info("[戳一戳] 通过 get_registered_star 获取 MemeManager 成功")
                            return self._meme_manager
                except Exception as e:
                    logger.debug(f"方案1失败: {e}")
            
            # 方案2: 通过 get_all_stars 遍历
            if hasattr(self.context, 'get_all_stars'):
                try:
                    all_stars = self.context.get_all_stars()
                    for star_meta in all_stars:
                        if hasattr(star_meta, 'name') and 'meme_generator' in str(star_meta.name).lower():
                            if hasattr(star_meta, 'star_cls') and star_meta.star_cls:
                                if hasattr(star_meta.star_cls, 'meme_manager'):
                                    self._meme_manager = star_meta.star_cls.meme_manager
                                    logger.info("[戳一戳] 通过 get_all_stars 获取 MemeManager 成功")
                                    return self._meme_manager
                except Exception as e:
                    logger.debug(f"方案2失败: {e}")
            
            # 方案3: 尝试从模块导入
            try:
                import sys
                for module_name, module in sys.modules.items():
                    if 'meme_generator' in module_name.lower():
                        # 检查模块中是否有全局实例
                        if hasattr(module, 'meme_manager'):
                            self._meme_manager = module.meme_manager
                            logger.info("[戳一戳] 通过模块导入获取 MemeManager")
                            return self._meme_manager
            except Exception as e:
                logger.debug(f"方案3失败: {e}")
            
            logger.warning("[戳一戳] 未找到 MemeManager，将使用回退方案")
            return None
            
        except Exception as e:
            logger.error(f"[戳一戳] 获取 MemeManager 异常: {e}")
            return None

    async def _init_meme_templates(self):
        """初始化模板缓存"""
        await asyncio.sleep(2)
        meme_manager = self._get_meme_manager()
        if meme_manager:
            await self._refresh_meme_templates()

    async def _refresh_meme_templates(self):
        """刷新模板缓存"""
        meme_manager = self._get_meme_manager()
        if not meme_manager:
            return
        
        now = time.time()
        if now - self._meme_templates.last_refresh < self._meme_templates.refresh_interval:
            return
        
        try:
            all_memes = await meme_manager.template_manager.get_all_memes()
            
            avatar_memes = []
            text_memes = []
            
            for meme in all_memes:
                params = meme.info.params
                if params.min_images >= 1:
                    avatar_memes.append(meme)
                elif params.max_texts >= 1:
                    text_memes.append(meme)
            
            self._meme_templates.avatar_templates = avatar_memes
            self._meme_templates.text_templates = text_memes
            self._meme_templates.last_refresh = now
            
            logger.info(
                f"[戳一戳] 模板缓存已刷新 - 头像类: {len(avatar_memes)}, 文字类: {len(text_memes)}"
            )
        except Exception as e:
            logger.error(f"[戳一戳] 刷新模板缓存失败: {e}")

    async def _get_cached_avatar(self, user_id: str) -> Optional[bytes]:
        """获取缓存头像或下载新头像"""
        # 检查缓存
        cached = self._avatar_cache.get(user_id)
        if cached:
            return cached
        
        # 下载头像
        meme_manager = self._get_meme_manager()
        if not meme_manager or not hasattr(meme_manager, 'network_utils'):
            return None
        
        network_utils = meme_manager.network_utils
        if not network_utils or not hasattr(network_utils, 'get_avatar'):
            return None
        
        try:
            avatar = await network_utils.get_avatar(user_id)
            if avatar and self.cfg.meme.cache_avatar:
                self._avatar_cache.add(user_id, avatar)
                await self._avatar_cache.cleanup_check()
            return avatar
        except Exception as e:
            logger.debug(f"[戳一戳] 获取头像失败: {e}")
            return None

    async def _generate_meme_image(self, event: AiocqhttpMessageEvent, user_id: str) -> Optional[bytes]:
        """使用 MemeGenerator 生成表情包"""
        
        # 冷却检查
        now = time.time()
        cooldown = getattr(self.cfg.meme, 'cooldown', 3)
        if now - self._meme_cooldown.get(user_id, 0) < cooldown:
            return None
        
        meme_manager = self._get_meme_manager()
        if not meme_manager:
            return None
        
        # 检查资源状态
        if hasattr(meme_manager, 'resource_status'):
            rs = meme_manager.resource_status
            if hasattr(rs, 'ready') and not rs.ready:
                logger.debug("[戳一戳] MemeGenerator 资源未就绪")
                return None
        
        # 刷新模板缓存
        await self._refresh_meme_templates()
        
        avatar_templates = self._meme_templates.avatar_templates
        text_templates = self._meme_templates.text_templates
        
        if not avatar_templates and not text_templates:
            return None
        
        # 选择模板类型
        prefer_avatar = getattr(self.cfg.meme, 'prefer_avatar_meme', True)
        avatar_ratio = getattr(self.cfg.meme, 'avatar_meme_ratio', 0.7)
        
        use_avatar = (
            avatar_templates and 
            (prefer_avatar and random.random() < avatar_ratio or not text_templates)
        )
        
        try:
            if use_avatar:
                template = random.choice(avatar_templates)
                image = await self._generate_avatar_meme(event, meme_manager, template, user_id)
            else:
                template = random.choice(text_templates)
                image = await self._generate_text_meme(event, meme_manager, template)
            
            if image:
                self._meme_cooldown[user_id] = now
                return image
            
        except Exception as e:
            logger.error(f"[戳一戳] 生成表情包失败: {e}")
        
        return None

    async def _generate_avatar_meme(self, event, meme_manager, template, user_id: str) -> Optional[bytes]:
        """生成头像类表情包"""
        try:
            from meme_generator import Image as MemeImage
        except ImportError:
            return None
        
        avatar = await self._get_cached_avatar(user_id)
        if not avatar:
            return None
        
        sender_name = event.get_sender_name() or str(user_id)
        meme_images = [MemeImage(sender_name, avatar)]
        
        default_texts = getattr(self.cfg.meme, 'default_texts', ["戳我干嘛"])
        # 根据模板参数决定是否需要文字
        params = template.info.params
        min_texts = params.min_texts
        max_texts = params.max_texts
        
        if min_texts == 0 and max_texts == 0:
            texts = []
        else:
            text_count = max(1, min_texts)
            texts = [random.choice(default_texts) for _ in range(text_count)]
        
        try:
            timeout = getattr(meme_manager.config, 'generation_timeout', 30)
            image_generator = meme_manager.image_generator
            
            if hasattr(image_generator, 'generate_image'):
                return await image_generator.generate_image(
                    template, meme_images, texts, {"name": sender_name}, timeout
                )
        except Exception as e:
            logger.error(f"[戳一戳] 头像表情包生成失败: {e}")
        
        return None

    async def _generate_text_meme(self, event, meme_manager, template) -> Optional[bytes]:
        """生成文字类表情包"""
        sender_name = event.get_sender_name() or "某人"
        default_texts = getattr(self.cfg.meme, 'default_texts', ["戳我干嘛"])
        # 根据模板参数生成正确数量的文字
        params = template.info.params
        min_texts = params.min_texts
        max_texts = params.max_texts
        default_tpl_texts = params.default_texts
        
        # 第一个文字变量必须是昵称（QQ昵称或群昵称）
        texts = [sender_name]
        
        # 用模板默认文字填充剩余位置（跳过第一个，因为已经是昵称）
        if default_tpl_texts and len(default_tpl_texts) > 1:
            texts.extend(default_tpl_texts[1:])
        
        # 如果还不够最小数量，用随机预设文字填充
        while len(texts) < min_texts:
            texts.append(random.choice(default_texts))
        
        # 如果超过最大数量，截断
        if max_texts > 0 and len(texts) > max_texts:
            texts = texts[:max_texts]
        
        try:
            timeout = getattr(meme_manager.config, 'generation_timeout', 30)
            image_generator = meme_manager.image_generator
            
            if hasattr(image_generator, 'generate_image'):
                return await image_generator.generate_image(
                    template, [], texts, {"name": sender_name}, timeout
                )
        except Exception as e:
            logger.error(f"[戳一戳] 文字表情包生成失败: {e}")
        
        return None

    async def handle(self, event: AiocqhttpMessageEvent):
        """响应戳一戳事件"""
        if not self._modules:
            return

        if event.get_extra("is_poked"):
            return
        evt = PokeEvent.from_event(event)
        logger.info(f"[DEBUG] is_poked check: {event.get_extra("is_poked")}")
        if not evt:
            return

        if evt.is_self_send:
            return

        if not evt.is_self_poked and random.random() < self.cfg.follow_prob:
            await self.sender.event_send(event, target_ids=[evt.target_id], times=1)
            return

        logger.info(f"[DEBUG] is_self_poked={evt.is_self_poked}, self_id={evt.self_id}, target_id={evt.target_id}")
        if not evt.is_self_poked:
            return
        
        user_id = evt.user_id
        current_time = time.time()
        
        if poke_ban_manager.is_banned(user_id):
            return
        
        last_time = self._last_poke_time.get(user_id, 0)
        if current_time - last_time > self._poke_reset_interval:
            self._poke_count[user_id] = 0
        
        self._last_poke_time[user_id] = current_time
        self._poke_count[user_id] = self._poke_count.get(user_id, 0) + 1
        count = self._poke_count[user_id]
        
        if count >= self._poke_threshold:
            self._poke_count.pop(user_id, None)
            self._last_poke_time.pop(user_id, None)
            
            try:
                raw_name = await get_nickname(event.bot, evt.group_id, user_id)
            except Exception:
                raw_name = event.get_sender_name() or str(user_id)
            
            #klee_name = get_klee_style_name(raw_name)
            klee_name = raw_name
            
            angry_messages = [
                f"唔...{klee_name}戳了可莉太多次了，可莉生气了不理你了！哼！十分钟内不理你！",
                f"哇！{klee_name}太烦了啦，可莉不想理你了十分钟！哼哼！",
                f"呜...{klee_name}老是戳可莉，可莉委屈...十分钟内不想理你了！",
                f"哼！{klee_name}再戳可莉就不和你玩了！十分钟内不理你！",
                f"呜哇——{klee_name}你是坏蛋！戳这么多下！可莉生气了，十分钟不理你！",
            ]
            
            angry_msg = random.choice(angry_messages)
            logger.info(f"[戳一戳] 用户 {raw_name} 连续戳了 {count} 次，发送生气消息")
            
            yield event.plain_result(angry_msg)
            event.stop_event()
            
            poke_ban_manager.add_ban(user_id, self._poke_ban_duration)
            return

        module = random.choices(self._modules, self._weights, k=1)[0]
        handler = self.handlers[module]

        try:
            async for msg in handler(event):
                if msg is not None:
                    yield msg
        except Exception as e:
            logger.error(f"执行戳一戳响应失败: {e}", exc_info=True)

    async def respond_poke(self, event: AiocqhttpMessageEvent):
        """反戳"""
        await self.sender.event_send(
            event,
            target_ids=[event.get_sender_id()],
            times=self.cfg.get_antipoke_times(),
        )
        event.stop_event()
        yield None

    async def respond_llm(self, event: AiocqhttpMessageEvent):
        """调用llm回复"""
        template = self.cfg.llm.template
        prompt = await self.llm.build_prompt(event, template)
        conversation = await self.llm.get_conversation(event)
        yield event.request_llm(prompt=prompt, conversation=conversation)

    async def respond_face(self, event: AiocqhttpMessageEvent):
        """回复emoji(QQ表情)"""
        face_id = self.cfg.get_face()
        copy_count = self.cfg.get_face_copy_count()
        faces_chain: list[Face] = [Face(id=face_id)] * copy_count
        yield event.chain_result(faces_chain)

    async def respond_meme(self, event: AiocqhttpMessageEvent):
        """回复表情包 - 优先 MemeGenerator，失败回退静态图片池"""
        
        user_id = event.get_sender_id()
        
        # 尝试 MemeGenerator
        meme_image = await self._generate_meme_image(event, user_id)
        
        if meme_image:
            logger.debug(f"[戳一戳] 成功生成 MemeGenerator 表情包，用户: {user_id}")
            yield event.chain_result([CompImage.fromBytes(meme_image)])
            return
        
        # 回退1：静态图片池
        static_image = self.cfg.get_image()
        if static_image:
            logger.debug(f"[戳一戳] 回退到静态图片池，用户: {user_id}")
            yield event.image_result(static_image)
            return
        
        # 回退2：QQ表情池
        fallback_to_face = getattr(self.cfg.meme, 'fallback_to_face', True)
        if fallback_to_face:
            face_id = self.cfg.get_face()
            copy_count = self.cfg.get_face_copy_count()
            faces_chain = [Face(id=face_id)] * copy_count
            logger.debug(f"[戳一戳] 回退到 QQ 表情，用户: {user_id}")
            yield event.chain_result(faces_chain)
            return
        
        logger.warning(f"[戳一戳] 所有表情回复方式都失败，用户: {user_id}")
        yield None

    async def respond_record(self, event: AiocqhttpMessageEvent):
        """回复语音"""
        audio_path = self.cfg.get_record()
        if audio_path:
            yield event.chain_result([Record(file=audio_path, url=audio_path)])
        else:
            yield None

    async def respond_ban(self, event: AiocqhttpMessageEvent):
        """禁言"""
        cfg = self.cfg.ban
        try:
            await event.bot.set_group_ban(
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                duration=self.cfg.get_ban_time(),
            )
            template = cfg.ban_template

        except Exception:
            template = cfg.ban_fail_template
        finally:
            prompt = await self.llm.build_prompt(event, template)
            conversation = await self.llm.get_conversation(event)
            yield event.request_llm(prompt=prompt, conversation=conversation)

    async def respond_cmd(self, event: AiocqhttpMessageEvent):
        """调用命令"""
        evt = copy.copy(event)
        evt.message_obj = copy.copy(event.message_obj)
        evt._extras = dict(event.get_extra())
        evt.clear_result()
        event.stop_event()

        cmd = self.cfg.get_command()

        evt.message_obj.message = [At(qq=evt.get_self_id()), Plain(cmd)]
        evt.message_obj.message_str = cmd

        evt.is_at_or_wake_command = True
        evt.message_str = cmd
        evt.should_call_llm(True)
        evt.set_extra("is_poked", True)

        self.context.get_event_queue().put_nowait(evt)
        yield None