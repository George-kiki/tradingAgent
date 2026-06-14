"""推送分发器：按配置的渠道列表统一发送。"""
from __future__ import annotations

from core.config import settings
from notify.channels import ALL_CHANNELS
from notify.base import Notifier


def get_notifiers() -> list[Notifier]:
    """根据 PUSH_CHANNELS 配置实例化可用渠道。"""
    notifiers = []
    for name in settings.channels:
        cls = ALL_CHANNELS.get(name)
        if cls:
            n = cls()
            if n.available():
                notifiers.append(n)
    if not notifiers:  # 兜底用控制台
        notifiers.append(ALL_CHANNELS["console"]())
    return notifiers


def push(title: str, content: str) -> dict:
    """向所有已配置渠道推送，返回各渠道结果。"""
    results = {}
    for n in get_notifiers():
        try:
            results[n.name] = n.send(title, content)
        except Exception as e:
            results[n.name] = False
            print(f"[{n.name} 推送异常] {e}")
    return results
