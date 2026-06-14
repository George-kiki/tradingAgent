"""具体推送渠道：控制台、企业微信、飞书、邮箱。"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.header import Header

from core.config import settings
from notify.base import Notifier


class ConsoleNotifier(Notifier):
    """控制台输出（默认，便于本地调试）。"""
    name = "console"

    def available(self) -> bool:
        return True

    def send(self, title: str, content: str) -> bool:
        print("\n" + "=" * 60)
        print(f"【{title}】")
        print("=" * 60)
        print(content)
        print("=" * 60 + "\n")
        return True


class WeChatWorkNotifier(Notifier):
    """企业微信群机器人（Markdown 消息）。"""
    name = "wechat"

    def available(self) -> bool:
        return bool(settings.wechat_webhook)

    def send(self, title: str, content: str) -> bool:
        if not self.available():
            return False
        # 企业微信 markdown 上限 4096 字节，超出截断
        text = f"# {title}\n\n{content}"
        if len(text.encode("utf-8")) > 4000:
            text = text[:1800] + "\n\n...(内容过长已截断)"
        try:
            import requests
            r = requests.post(
                settings.wechat_webhook,
                json={"msgtype": "markdown", "markdown": {"content": text}},
                timeout=10,
            )
            return r.status_code == 200 and r.json().get("errcode") == 0
        except Exception as e:
            print(f"[企业微信推送失败] {e}")
            return False


class FeishuNotifier(Notifier):
    """飞书自定义机器人（富文本/文本）。"""
    name = "feishu"

    def available(self) -> bool:
        return bool(settings.feishu_webhook)

    def send(self, title: str, content: str) -> bool:
        if not self.available():
            return False
        try:
            import requests
            r = requests.post(
                settings.feishu_webhook,
                json={"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}},
                timeout=10,
            )
            return r.status_code == 200 and r.json().get("StatusCode", 0) == 0 or r.json().get("code", -1) == 0
        except Exception as e:
            print(f"[飞书推送失败] {e}")
            return False


class EmailNotifier(Notifier):
    """SMTP 邮箱推送。"""
    name = "email"

    def available(self) -> bool:
        return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password and settings.smtp_to)

    def send(self, title: str, content: str) -> bool:
        if not self.available():
            return False
        recipients = [x.strip() for x in settings.smtp_to.split(",") if x.strip()]
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = settings.smtp_user
        msg["To"] = ",".join(recipients)
        try:
            if settings.smtp_port == 465:
                server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
                server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, recipients, msg.as_string())
            server.quit()
            return True
        except Exception as e:
            print(f"[邮箱推送失败] {e}")
            return False


ALL_CHANNELS = {
    "console": ConsoleNotifier,
    "wechat": WeChatWorkNotifier,
    "feishu": FeishuNotifier,
    "email": EmailNotifier,
}
