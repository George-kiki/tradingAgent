"""推送渠道基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """该渠道是否已正确配置。"""

    @abstractmethod
    def send(self, title: str, content: str) -> bool:
        """发送消息，content 为 Markdown 文本。返回是否成功。"""
