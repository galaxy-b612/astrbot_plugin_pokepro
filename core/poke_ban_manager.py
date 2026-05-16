"""
戳一戳临时黑名单管理器
通过文件共享黑名单状态，让其他插件可以访问
"""
import json
import time
import os
from pathlib import Path
from astrbot.api import logger


class PokeBanManager:
    """戳一戳临时黑名单管理器"""
    
    _instance = None
    _ban_file = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._ban_file = Path("/root/astrbot/data/temp/poke_ban_list.json")
            cls._ban_file.parent.mkdir(parents=True, exist_ok=True)
        return cls._instance
    
    def add_ban(self, user_id: str, duration_seconds: int = 600):
        """添加用户到临时黑名单"""
        ban_list = self._load_ban_list()
        ban_end = time.time() + duration_seconds
        ban_list[user_id] = ban_end
        self._save_ban_list(ban_list)
        logger.info(f"[戳一戳黑名单] 用户 {user_id} 已添加到黑名单，{duration_seconds}秒后解除")
    
    def remove_ban(self, user_id: str):
        """从临时黑名单移除用户"""
        ban_list = self._load_ban_list()
        if user_id in ban_list:
            del ban_list[user_id]
            self._save_ban_list(ban_list)
            logger.info(f"[戳一戳黑名单] 用户 {user_id} 已从黑名单移除")
    
    def is_banned(self, user_id: str) -> bool:
        """检查用户是否在黑名单中"""
        ban_list = self._load_ban_list()
        if user_id not in ban_list:
            return False
        
        # 检查是否过期
        if time.time() < ban_list[user_id]:
            return True
        else:
            # 已过期，移除
            del ban_list[user_id]
            self._save_ban_list(ban_list)
            return False
    
    def get_remaining_time(self, user_id: str) -> int:
        """获取剩余黑名单时间（秒）"""
        ban_list = self._load_ban_list()
        if user_id not in ban_list:
            return 0
        remaining = int(ban_list[user_id] - time.time())
        return max(0, remaining)
    
    def _load_ban_list(self) -> dict:
        """加载黑名单"""
        try:
            if self._ban_file.exists():
                with open(self._ban_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"[戳一戳黑名单] 加载失败: {e}")
        return {}
    
    def _save_ban_list(self, ban_list: dict):
        """保存黑名单"""
        try:
            with open(self._ban_file, 'w') as f:
                json.dump(ban_list, f)
        except Exception as e:
            logger.error(f"[戳一戳黑名单] 保存失败: {e}")


# 全局单例
poke_ban_manager = PokeBanManager()