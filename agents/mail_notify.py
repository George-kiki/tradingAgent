"""热点捕获任务邮箱提醒模块。

每次热点驱动扫描前后，向指定邮箱发送预告/完成通知。
使用 Python 标准库 smtplib，零额外依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

# ── 计数文件路径 ──
_COUNT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scan_count.json")


def _load_env_config() -> dict:
    """从环境变量加载邮件配置。"""
    return {
        "enabled": os.getenv("MAIL_ENABLED", "false").lower() == "true",
        "host": os.getenv("MAIL_SMTP_HOST", "smtp.qq.com"),
        "port": int(os.getenv("MAIL_SMTP_PORT", "465")),
        "user": os.getenv("MAIL_SMTP_USER", ""),
        "password": os.getenv("MAIL_SMTP_PASSWORD", ""),
        "to": os.getenv("MAIL_TO", "504110744@qq.com"),
        "from_addr": os.getenv("MAIL_FROM", os.getenv("MAIL_SMTP_USER", "504110744@qq.com")),
    }


def should_notify() -> bool:
    """检查是否启用邮件提醒。"""
    cfg = _load_env_config()
    if not cfg["enabled"]:
        return False
    if not cfg["password"]:
        print("[邮件提醒] MAIL_SMTP_PASSWORD 未配置，跳过")
        return False
    if not cfg["to"]:
        print("[邮件提醒] MAIL_TO 未配置，跳过")
        return False
    return True


def get_scan_count() -> tuple[int, str]:
    """读取当前执行计数。返回 (count, last_scan)。"""
    try:
        with open(_COUNT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("count", 0), data.get("last_scan", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return 0, ""


def increment_scan_count(exec_time: dt.datetime | None = None) -> int:
    """递增执行计数并持久化。返回新的 count。"""
    count, _ = get_scan_count()
    count += 1
    ts = (exec_time or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    data = {"count": count, "last_scan": ts}
    os.makedirs(os.path.dirname(_COUNT_FILE), exist_ok=True)
    with open(_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return count


def _send_mail(to: str, subject: str, html: str, timeout: int = 30) -> bool:
    """通过 SMTP 发送一封 HTML 邮件。成功返回 True，失败打印日志并返回 False。"""
    cfg = _load_env_config()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = cfg["from_addr"]
        msg["To"] = to
        msg.attach(MIMEText(html, "html", "utf-8"))

        smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout)
        smtp.login(cfg["user"], cfg["password"])
        smtp.sendmail(cfg["from_addr"], [to], msg.as_string())
        smtp.quit()
        print(f"[邮件提醒] ✅ 已发送 → {to}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("[邮件提醒] ❌ SMTP 认证失败，请检查 MAIL_SMTP_PASSWORD（QQ邮箱需使用授权码）")
    except smtplib.SMTPException as e:
        print(f"[邮件提醒] ❌ SMTP 错误: {e}")
    except Exception as e:
        print(f"[邮件提醒] ❌ 发送失败: {e}")
    return False


def send_scan_pre_notify(count: int, exec_time: dt.datetime, to_addr: str = "") -> bool:
    """扫描前预告邮件。"""
    cfg = _load_env_config()
    recipient = to_addr or cfg["to"]
    ts = exec_time.strftime("%Y年%m月%d日 %H:%M")

    subject = f"【热点捕获预告】第{count}次扫描即将执行"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, system-ui, sans-serif; background: #f0f2f5; padding: 20px;">
<div style="max-width: 500px; margin: 0 auto; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.08);">
  <div style="background: linear-gradient(135deg, #4facfe, #00f2fe); padding: 24px; text-align: center;">
    <div style="font-size: 32px;">📣</div>
    <div style="font-size: 18px; font-weight: 700; color: #fff; margin-top: 8px;">热点捕获任务预告</div>
  </div>
  <div style="padding: 24px;">
    <table style="width: 100%; border-collapse: collapse;">
      <tr>
        <td style="padding: 10px 0; color: #666; width: 80px;">任务编号</td>
        <td style="padding: 10px 0; font-weight: 600;">第 {count} 次</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #666;">执行时间</td>
        <td style="padding: 10px 0; font-weight: 600;">{ts}</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #666;">任务内容</td>
        <td style="padding: 10px 0;">全球热点新闻多渠道抓取 + 多Agent分析 + 板块映射</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #666;">预计耗时</td>
        <td style="padding: 10px 0;">3-8 分钟</td>
      </tr>
    </table>
  </div>
  <div style="background: #f8f9fa; padding: 16px 24px; font-size: 12px; color: #999; text-align: center; border-top: 1px solid #eee;">
    此邮件由 AI-Agent 智能分析系统自动发送 · {ts}
  </div>
</div>
</body></html>"""

    print(f"[邮件提醒] 发送预告 → 第{count}次扫描 @ {ts}")
    return _send_mail(recipient, subject, html)


def send_scan_done_notify(count: int, duration_min: float, result: dict,
                          to_addr: str = "") -> bool:
    """扫描完成后通知邮件。"""
    cfg = _load_env_config()
    recipient = to_addr or cfg["to"]
    now = dt.datetime.now().strftime("%Y年%m月%d日 %H:%M")
    dur_disp = f"{duration_min:.1f}" if duration_min < 1 else f"{int(duration_min)}"

    # 从结果提取摘要
    sectors = result.get("sectors", []) if result else []
    total_news = result.get("total_news", 0) if result else 0
    summary = result.get("summary", "") if result else ""

    # 构建板块列表
    sector_rows = ""
    if sectors:
        for i, s in enumerate(sectors[:5]):
            name = s.get("name", "?")
            direction = s.get("direction", "—")
            score = s.get("score", 0)
            if isinstance(score, (int, float)):
                score_str = f"{score:.0f}分"
            else:
                score_str = str(score)
            sector_rows += f"""<tr>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0;">{i+1}</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0; font-weight: 600;">{name}</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0;">{direction}</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0;">{score_str}</td>
      </tr>"""

    sector_table = ""
    if sector_rows:
        sector_table = f"""<table style="width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px;">
      <tr style="color: #999; font-size: 12px;">
        <td style="padding: 6px 0; border-bottom: 1px solid #ddd;">#</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #ddd;">板块</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #ddd;">方向</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #ddd;">评分</td>
      </tr>
      {sector_rows}
    </table>"""

    subject = f"【热点捕获完成】第{count}次扫描已完成"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, system-ui, sans-serif; background: #f0f2f5; padding: 20px;">
<div style="max-width: 500px; margin: 0 auto; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.08);">
  <div style="background: linear-gradient(135deg, #43e97b, #38f9d7); padding: 24px; text-align: center;">
    <div style="font-size: 32px;">✅</div>
    <div style="font-size: 18px; font-weight: 700; color: #fff; margin-top: 8px;">热点捕获任务完成</div>
  </div>
  <div style="padding: 24px;">
    <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
      <tr>
        <td style="padding: 10px 0; color: #666; width: 80px;">任务编号</td>
        <td style="padding: 10px 0; font-weight: 600;">第 {count} 次</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #666;">完成时间</td>
        <td style="padding: 10px 0; font-weight: 600;">{now}</td>
      </tr>
      <tr>
        <td style="padding: 10px 0; color: #666;">实际耗时</td>
        <td style="padding: 10px 0; font-weight: 600;">{dur_disp} 分钟</td>
      </tr>
    </table>

    <div style="background: #f8f9fa; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
      <div style="font-size: 15px; font-weight: 700; margin-bottom: 12px;">📊 结果小结</div>
      <div style="font-size: 14px; line-height: 1.8;">
        <div>📰 抓取新闻：<b>{total_news} 条</b></div>
        <div>📈 利好板块：<b>{len(sectors)} 个</b></div>
        {f'<div style="margin-top: 4px;">📝 {summary[:200]}</div>' if summary else ''}
      </div>
      {sector_table}
    </div>
  </div>
  <div style="background: #f8f9fa; padding: 16px 24px; font-size: 12px; color: #999; text-align: center; border-top: 1px solid #eee;">
    此邮件由 AI-Agent 智能分析系统自动发送 · {now}
  </div>
</div>
</body></html>"""

    print(f"[邮件提醒] 发送完成通知 → 第{count}次扫描，耗时{dur_disp}分钟")
    return _send_mail(recipient, subject, html)


def send_test_mail(to: str = "") -> bool:
    """发送测试邮件，用于验证配置是否正确。"""
    cfg = _load_env_config()
    recipient = to or cfg["to"]
    now = dt.datetime.now().strftime("%Y年%m月%d日 %H:%M")
    subject = "【AI-Agent】邮箱提醒功能测试"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, system-ui, sans-serif; padding: 20px; background: #f0f2f5;">
<div style="max-width: 400px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 2px 12px rgba(0,0,0,.08);">
  <div style="font-size: 32px; text-align: center;">✅</div>
  <div style="font-size: 18px; font-weight: 700; text-align: center; margin: 12px 0;">邮箱配置成功！</div>
  <div style="color: #666; text-align: center;">热点捕获邮箱提醒已就绪</div>
  <div style="color: #999; font-size: 12px; text-align: center; margin-top: 16px;">{now}</div>
</div>
</body></html>"""
    return _send_mail(recipient, subject, html)
