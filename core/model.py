from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class PokeModel(Enum):
    ANTIPOKE = "antipoke"
    LLM = "llm"
    FACE = "face"
    MEME = "meme"
    RECORD = "record"
    BAN = "ban"
    COMMAND = "command"

    def __str__(self) -> str:
        return self.value


@dataclass
class PokeEvent:
    """戳一戳事件"""

    time: int
    self_id: int
    user_id: int
    target_id: int
    group_id: int | None

    # OneBot / notice 语义字段
    post_type: str
    notice_type: str
    sub_type: str

    # 原始附加信息
    raw_info: list[dict[str, Any]]

    @classmethod
    def from_event(cls, event) -> Optional["PokeEvent"]:
        msg = getattr(event, "message_obj", None)
        raw = getattr(msg, "raw_message", None) if msg else None
        from astrbot.api import logger as _logger
        _logger.info(f"[DEBUG PokeEvent] msg={msg is not None}, raw_type={type(raw).__name__ if raw else None}")
        
        # 辅助函数：安全转换为整数
        def safe_int(value, default=0):
            if value is None:
                return default
            try:
                return int(value)
            except (ValueError, TypeError):
                return default
        if isinstance(raw, dict):
            _logger.info(f"[DEBUG PokeEvent] raw_keys={list(raw.keys())} post_type={raw.get('post_type')} notice_type={raw.get('notice_type')} sub_type={raw.get('sub_type')}")
        if not isinstance(raw, dict):
            _logger.warning("[DEBUG PokeEvent] raw is not dict, returning None")
            return None

        if raw.get("post_type") != "notice":
            return None
        if raw.get("notice_type") != "notify":
            return None
        if raw.get("sub_type") != "poke":
            return None

        self_id_val = safe_int(raw.get("self_id"), 0)
        target_id_val = safe_int(raw.get("target_id"), 0)
        user_id_val = safe_int(raw.get("user_id"), 0)
        _logger.info(f"[PokeEvent] 原始数据 - self_id={self_id_val} (type={type(self_id_val).__name__}), target_id={target_id_val} (type={type(target_id_val).__name__})")
        
        return cls(
            time=safe_int(raw.get("time"), 0),
            self_id=self_id_val,
            user_id=user_id_val,
            target_id=target_id_val,
            group_id=raw.get("group_id"),
            post_type=raw.get("post_type", ""),
            notice_type=raw.get("notice_type", ""),
            sub_type=raw.get("sub_type", ""),
            raw_info=raw.get("raw_info", []),
        )

    # ========= 语义属性 =========

    @property
    def is_self_poked(self) -> bool:
        """是否戳的是机器人自己"""
        return self.self_id == self.target_id

    @property
    def is_self_send(self) -> bool:
        """是否为机器人自己发出的戳"""
        return self.user_id == self.self_id

    @property
    def is_group_poke(self) -> bool:
        """是否群戳"""
        return self.group_id is not None

    @property
    def is_private_poke(self) -> bool:
        """是否私聊戳（极少，但语义完整）"""
        return self.group_id is None


# 示例原始数据
# raw = {
#     "time": 1770684953,
#     "self_id": 1959676873,
#     "post_type": "notice",
#     "notice_type": "notify",
#     "sub_type": "poke",
#     "target_id": 1959676873,
#     "user_id": 2936169201,
#     "group_id": 952212291,
#     "raw_info": [
#         {"col": "1", "nm": "", "type": "qq", "uid": "u_QmVcCfvoEUKZv6rb2WM7Lw"},
#         {
#             "jp": "https://zb.vip.qq.com/v2/pages/nudgeMall?_wv=2&actionId=0&effectId=5",
#             "src": "http://tianquan.gtimg.cn/nudgeeffect/item/5/client.gif",
#             "type": "img",
#         },
#         {
#             "col": "1",
#             "nm": "",
#             "tp": "0",
#             "type": "qq",
#             "uid": "u_4Twr4XaJJ8CPkZI5hKOPsw",
#         },
#         {"txt": "的服务器", "type": "nor"},
#     ],
# }
