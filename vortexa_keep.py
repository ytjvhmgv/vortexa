#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Vortexa 免费主机自动续期。

登录 api.vortexa.cloud 并上报免费服务的活跃度，满足「2 天内活跃一次」的续期要求。
"""

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

import requests

# ── 规格固定的常量 ──────────────────────────────────────────────
API_BASE = "https://api.vortexa.cloud/api"
ORIGIN = "https://www.vortexa.cloud"
FREE_TTL_HOURS = 48  # 仅用于通知文案，不参与控制流判断
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 出口由 CI 的 WARP 接管（fscarmen/warp-on-actions，mode=client，stack=dual，
# warp+doh）。脚本直连，不设置 HTTP/SOCKS 代理。


@dataclass
class Config:
    """运行期配置，全部来自环境变量。"""

    email: str
    password: str
    tg_bot_token: str
    tg_chat_id: str


def load_config(env: Mapping[str, str]) -> Optional[Config]:
    """从环境变量读取配置；缺少邮箱或密码时返回 None。"""
    email = (env.get("VORTEXA_EMAIL") or "").strip()
    password = env.get("VORTEXA_PASSWORD") or ""
    if not email or not password:
        return None
    return Config(
        email=email,
        password=password,
        tg_bot_token=env.get("TG_BOT_TOKEN") or "",
        tg_chat_id=env.get("TG_CHAT_ID") or "",
    )


def mask_email(email: str) -> str:
    """脱敏邮箱，用于日志与通知。"""
    if not email:
        return ""
    if "@" not in email:
        return email[:2] + "****"
    name, domain = email.split("@", 1)
    if len(name) > 4:
        name = f"{name[:2]}****{name[-2:]}"
    return f"{name}@{domain}"


def build_session() -> requests.Session:
    """构造带浏览器特征请求头的 Session（承载登录种下的 cookie）。

    CI 上由 WARP client 接管出口，这里不设置 proxies。
    """
    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/login",
            "User-Agent": USER_AGENT,
        }
    )
    return session


def api_request(
    session: requests.Session,
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    json_body: Optional[dict] = None,
    timeout: int = 30,
) -> tuple:
    """统一的 API 调用入口，返回 (status_code, payload)。

    payload 在响应可解析为 JSON 时为 dict/list，否则为原始文本；网络异常返回 (0, 异常描述)。
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = session.request(
            method, API_BASE + path, json=json_body, headers=headers, timeout=timeout
        )
    except Exception as exc:  # 网络异常
        return 0, str(exc)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return response.status_code, payload


def _error_from(status: int, payload) -> str:
    """从失败响应中抽取可读的错误信息。"""
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)
    return f"HTTP {status}: {str(payload)[:200]}"


def login(
    session: requests.Session, email: str, password: str
) -> tuple:
    """登录并返回 (accessToken, error)，二者必有一为 None。"""
    status, payload = api_request(
        session,
        "POST",
        "/auth/login",
        json_body={"email": email, "password": password},
    )
    if 200 <= status < 300 and isinstance(payload, dict) and payload.get("accessToken"):
        return payload["accessToken"], None
    return None, _error_from(status, payload)


def read_service_status(session: requests.Session, token: str) -> tuple:
    """读取免费服务状态，返回 (status_payload, error)。"""
    status, payload = api_request(session, "GET", "/hosting/free/status", token=token)
    if 200 <= status < 300 and isinstance(payload, dict):
        return payload, None
    return None, _error_from(status, payload)


def should_resume(status_payload: dict) -> bool:
    """判断免费服务是否处于需要 resume 的状态。"""
    service = (status_payload or {}).get("service") or {}
    if not service:
        return False
    return service.get("status") != "active" or bool(service.get("free_suspended_at"))


def resume_service(session: requests.Session, token: str) -> tuple:
    """恢复被暂停的免费服务，返回 (ok, error)。"""
    status, payload = api_request(session, "POST", "/hosting/free/resume", token=token)
    if 200 <= status < 300:
        return True, None
    return False, _error_from(status, payload)


def record_activity(session: requests.Session, token: str) -> tuple:
    """上报活跃度（真正的续期动作），返回 (payload, error)。

    仅看 HTTP 状态码不够：接口可能返回 2xx 但 success 为 false，或者缺少时间戳，
    这两种情况都必须当失败处理，否则会误报「续期成功」。
    """
    status, payload = api_request(session, "POST", "/hosting/free/activity", token=token)
    if not (200 <= status < 300) or not isinstance(payload, dict) or payload.get("success") is False:
        return None, _error_from(status, payload)
    data = payload.get("data") or {}
    if not isinstance(data.get("last_activity_at"), str):
        return None, f"activity 响应缺少 data.last_activity_at: {str(payload)[:200]}"
    return payload, None


def build_message(icon: str, title: str, email: str, body: str = "") -> str:
    """拼装通知文案（北京时间），邮箱一律脱敏。"""
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    text = (
        f"🇺🇸 Vortexa 保活通知\n\n"
        f"{icon} {title}\n"
        f"👤 登录账户: {mask_email(email)}\n"
        f"⏱️ 登录时间: {current_time_str}"
    )
    if body:
        text += f"\n\n{body}"
    return text


def send_tg_message(config: Config, icon: str, title: str, body: str = "") -> None:
    """推送 Telegram 通知；未配置或异常时只打印，不影响主流程。"""
    if not config.tg_bot_token or not config.tg_chat_id:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送。")
        return

    url = f"https://api.telegram.org/bot{config.tg_bot_token}/sendMessage"
    payload = {
        "chat_id": config.tg_chat_id,
        "text": build_message(icon, title, config.email, body),
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("📩 Telegram 通知发送成功！")
        else:
            print(f"  ⚠️ Telegram 通知发送失败: {response.text}")
    except Exception as exc:
        # 不打印 exc 原文：requests 的异常字符串内嵌完整 URL，会把 Bot Token 带进日志。
        print(f"  ⚠️ Telegram 通知发送异常: {type(exc).__name__}")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """解析接口返回的 ISO8601 时间戳，失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _beijing_str(moment: Optional[datetime]) -> str:
    """把时间点格式化为北京时间字符串。"""
    if moment is None:
        return "未知"
    return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def renew(session: requests.Session, token: str) -> dict:
    """执行一次续期：读状态 → 必要时 resume → 上报 activity。

    成功与否只看 activity 接口的返回，不回读 status（该接口有服务端缓存）。
    """
    status_payload, error = read_service_status(session, token)
    if error:
        return {"ok": False, "error": error}

    status_payload = status_payload or {}
    if status_payload.get("has_free_server") is False:
        return {"ok": False, "reason": "no_free_server"}

    service = status_payload.get("service") or {}
    if not service:
        # 字段缺失多半意味着接口改版，不能当成「无免费服务」静默退出 0，必须失败告警。
        return {"ok": False, "error": f"status 响应不符合预期: {str(status_payload)[:200]}"}

    resumed = False
    if should_resume(status_payload):
        ok, error = resume_service(session, token)
        if not ok:
            return {"ok": False, "error": error}
        resumed = True

    payload, error = record_activity(session, token)
    if error:
        return {"ok": False, "error": error}

    data = (payload or {}).get("data") or {}
    return {
        "ok": True,
        "resumed": resumed,
        "before": service.get("last_activity_at"),
        "after": data.get("last_activity_at"),
        "service_id": data.get("service_id") or service.get("id"),
    }


def main(argv=None) -> int:
    """入口：返回进程退出码（成功 0，失败非 0）。"""
    print("#" * 25)
    print("   Vortexa 免费主机自动续期")
    print("#" * 25)

    config = load_config(os.environ)
    if config is None:
        print("❌ 缺少 VORTEXA_EMAIL 或 VORTEXA_PASSWORD 环境变量")
        return 1

    print(f"👤 登录账户: {mask_email(config.email)}")

    session = build_session()

    token, error = login(session, config.email, config.password)
    if not token:
        print(f"❌ 登录失败: {error}")
        send_tg_message(config, "❌", "登录失败", f"错误: {error}")
        return 1
    print("✅ 登录成功")

    result = renew(session, token)

    if result.get("ok"):
        print(f"✅ 续期成功: {result.get('before')} → {result.get('after')}")
        deadline = _parse_iso(result.get("after"))
        if deadline:
            deadline = deadline + timedelta(hours=FREE_TTL_HOURS)
        body = (
            f"服务 ID: {result.get('service_id')}\n"
            f"续期前: {result.get('before')}\n"
            f"续期后: {result.get('after')}\n"
            f"已恢复暂停: {'是' if result.get('resumed') else '否'}\n"
            f"预计截止: {_beijing_str(deadline)}（按 {FREE_TTL_HOURS} 小时计）"
        )
        send_tg_message(config, "✅", "续期成功", body)
        return 0

    if result.get("reason") == "no_free_server":
        print("ℹ️ 账户没有免费服务，无需续期")
        send_tg_message(config, "ℹ️", "无免费服务可续期")
        return 0

    print(f"❌ 续期失败: {result.get('error')}")
    send_tg_message(config, "❌", "续期失败", f"错误: {result.get('error')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
