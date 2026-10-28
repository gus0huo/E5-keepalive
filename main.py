#!/usr/bin/env python3
"""
E5 KeepAlive — AutoApiSecret 风格的 E5 开发活跃度维持脚本
- 多 E5 账户：secret E5_ACCOUNTS（JSON 数组），单账户模式向后兼容
- 运行结果通知：GitHub Step Summary + Server酱 / Telegram / SMTP 邮件（任选）
- 通过 NOTIFY_ENABLED 控制是否推送（上午场静默，仅下午场推送）
- 推送附带订阅状态与到期时间（手动配置 / 从微软通知邮件推算）
- 写操作即建即删；404/507 跳过；429/5xx 退避重试
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

log = logging.getLogger("e5-keepalive")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    name: str
    tenant_id: str
    client_id: str
    client_secret: str
    users: list[str] = field(default_factory=list)  # 空 = 租户内前 25 个用户
    min_calls: int = 18
    max_calls: int = 36
    enable_writes: bool = True
    expiry: str = ""            # 手动指定到期日，如 "2027-01-20"
    expiry_scan_user: str = ""  # 扫描哪个邮箱的微软通知邮件（建议填管理员 UPN）


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def load_accounts() -> list[Account]:
    """优先读 E5_ACCOUNTS(JSON)；否则退回单账户三件套环境变量。"""
    raw = os.getenv("E5_ACCOUNTS", "").strip()
    default_min = _get_int("MIN_CALLS", 18)
    default_max = _get_int("MAX_CALLS", 36)
    default_writes = os.getenv("ENABLE_WRITES", "true").strip().lower() != "false"

    accounts: list[Account] = []
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("E5_ACCOUNTS 不是合法 JSON：%s", exc)
            sys.exit(2)
        for i, it in enumerate(items, 1):
            accounts.append(
                Account(
                    name=it.get("name") or f"账户{i}",
                    tenant_id=it.get("tenant_id", "") or "organizations",
                    client_id=it.get("client_id", ""),
                    client_secret=it.get("client_secret", ""),
                    users=[u.strip() for u in it.get("users", []) if u.strip()],
                    min_calls=int(it.get("min_calls", default_min)),
                    max_calls=int(it.get("max_calls", default_max)),
                    enable_writes=bool(it.get("enable_writes", default_writes)),
                    expiry=str(it.get("expiry", "")).strip(),
                    expiry_scan_user=str(it.get("expiry_scan_user", "")).strip(),
                )
            )
    else:
        env_users = [
            u.strip() for u in os.getenv("TARGET_USERS", "").split(",") if u.strip()
        ]
        accounts.append(
            Account(
                name="默认账户",
                tenant_id=os.getenv("AZURE_TENANT_ID") or "organizations",
                client_id=os.getenv("AZURE_CLIENT_ID", ""),
                client_secret=os.getenv("AZURE_CLIENT_SECRET", ""),
                users=env_users,
                min_calls=default_min,
                max_calls=default_max,
                enable_writes=default_writes,
                expiry=os.getenv("EXPIRY_DATE", "").strip(),
                expiry_scan_user=os.getenv("EXPIRY_SCAN_USER", "").strip(),
            )
        )

    valid = []
    for acc in accounts:
        if acc.client_id and acc.client_secret:
            if acc.min_calls > acc.max_calls:
                acc.min_calls, acc.max_calls = acc.max_calls, acc.min_calls
            valid.append(acc)
        else:
            log.warning("账户 %s 缺少 client_id/client_secret，已跳过", acc.name)
    return valid


# --------------------------------------------------------------------------- #
# Token 管理
# --------------------------------------------------------------------------- #
class TokenProvider:
    def __init__(self, acc: Account) -> None:
        self.acc = acc
        self._token = ""
        self._expires_at = 0.0

    def invalidate(self) -> None:
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token
        url = TOKEN_ENDPOINT.format(tenant=self.acc.tenant_id)
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.acc.client_id,
                "client_secret": self.acc.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"获取 token 失败: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        log.info("[%s] 已获取 access token", self.acc.name)
        return self._token


# --------------------------------------------------------------------------- #
# Graph 客户端（重试 + 统计）
# --------------------------------------------------------------------------- #
class GraphClient:
    def __init__(self, tokens: TokenProvider) -> None:
        self.tokens = tokens
        self.http = requests.Session()
        self.ok = 0
        self.fail = 0
        self.skip = 0

    def call(self, method: str, path: str, **kwargs) -> requests.Response | None:
        url = path if path.startswith("http") else GRAPH + path
        for attempt in range(1, 4):
            resp = self.http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.tokens.get()}"},
                timeout=30,
                **kwargs,
            )
            if resp.status_code == 401:
                log.warning("401，刷新 token 后重试")
                self.tokens.invalidate()
                continue
            if resp.status_code in (404, 507):
                self.skip += 1
                log.info(
                    "%s %s -> %s，跳过（对方账号问题，不影响结果）",
                    method, path, resp.status_code,
                )
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(int(resp.headers.get("Retry-After", 2 ** attempt)), 120)
                log.warning("%s %s -> %s，%ss 后重试", method, path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                self.fail += 1
                log.warning("%s %s -> %s %s", method, path, resp.status_code, resp.text[:200])
                return resp
            self.ok += 1
            log.info("%s %s -> %s", method, path, resp.status_code)
            return resp
        self.fail += 1
        log.error("%s %s 重试耗尽", method, path)
        return None


# --------------------------------------------------------------------------- #
# 订阅状态与到期时间
# --------------------------------------------------------------------------- #
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _plain_get(tokens: TokenProvider, path: str):
    """静默 GET：失败只记日志，不计入成功/失败统计，绝不影响主流程。"""
    try:
        resp = requests.get(
            GRAPH + path,
            headers={"Authorization": f"Bearer {tokens.get()}"},
            timeout=30,
        )
    except Exception as exc:
        log.info("订阅信息查询异常，已忽略: %s", exc)
        return None
    if resp.ok:
        return resp.json()
    log.info("订阅信息查询未成功（HTTP %s），已忽略", resp.status_code)
    return None


def _extract_date(text: str, base: datetime | None) -> str:
    """从文本提取日期：显式日期优先，其次 'in N days / N 天' 相对推算。"""
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+(20\d{2})\b",
        text,
        re.I,
    )
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    if base:
        m = re.search(r"in\s+(\d{1,3})\s+days", text, re.I)
        if not m:
            m = re.search(r"renewed\s+(?:for\s+)?(?:another\s+)?(\d{1,3})\s+days", text, re.I)
        if not m:
            m = re.search(r"(\d{1,3})\s*天", text)
        if m:
            return (base + timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return ""


def get_subscription_info(acc: Account, tokens: TokenProvider, fallback_uid: str):
    """返回 (订阅状态摘要, 到期时间线索)。查不到就返回空串。"""
    # 1) 订阅状态与席位
    sub = ""
    data = _plain_get(
        tokens,
        "/subscribedSkus?$select=skuPartNumber,capabilityStatus,consumedUnits,prepaidUnits",
    )
    if data:
        bits = []
        for s in data.get("value", []):
            enabled = (s.get("prepaidUnits") or {}).get("enabled") or 0
            bits.append(
                f"{s.get('skuPartNumber', '?')} {s.get('capabilityStatus', '?')}"
                f"（{s.get('consumedUnits', 0)}/{enabled} 席）"
            )
        sub = "；".join(bits)

    # 2) 到期时间：手动配置 > 邮件推算
    expiry = ""
    if acc.expiry:
        expiry = f"{acc.expiry}（手动配置）"
    else:
        uid = acc.expiry_scan_user or fallback_uid
        if uid:
            kw = "expires OR expiring OR expiration OR renewal OR renewed OR 续订 OR 续期 OR 到期"
            data = _plain_get(
                tokens,
                f"/users/{uid}/messages?$search=\"{kw}\"&$top=10"
                "&$select=subject,receivedDateTime,bodyPreview",
            )
            if data:
                msgs = data.get("value", [])
                msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
                for msg in msgs:
                    text = f"{msg.get('subject', '')} {msg.get('bodyPreview', '')}"
                    base = None
                    try:
                        base = datetime.fromisoformat(
                            msg.get("receivedDateTime", "").replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                    d = _extract_date(text, base)
                    if d:
                        src = base.strftime("%Y-%m-%d") if base else "未知日期"
                        expiry = f"{d}（据 {src} 的通知邮件推算）"
                        break
    return sub, expiry


# --------------------------------------------------------------------------- #
# 只读 API 列表
# --------------------------------------------------------------------------- #
def read_endpoints(uid: str) -> list[tuple[str, str]]:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        ("GET", f"/users/{uid}/messages?$top=5&$select=id,subject,receivedDateTime"),
        ("GET", f"/users/{uid}/mailFolders?$top=5&$select=id,displayName,totalItemCount"),
        ("GET", f"/users/{uid}/events?$top=5&$select=id,subject,start,end"),
        ("GET", f"/users/{uid}/calendarView?startDateTime={start}&endDateTime={end}&$top=5"),
        ("GET", f"/users/{uid}/drive?$select=id,driveType,quota"),
        ("GET", f"/users/{uid}/drive/root/children?$top=5&$select=id,name,size"),
        ("GET", f"/users/{uid}/photo/$value"),
        ("GET", f"/users/{uid}/memberOf?$top=5&$select=id,displayName"),
        ("GET", f"/users/{uid}/onenote/notebooks?$select=id,displayName"),
        ("GET", f"/users/{uid}/chats?$top=5&$select=id,topic"),
    ]


# --------------------------------------------------------------------------- #
# 可回滚的写操作
# --------------------------------------------------------------------------- #
def write_draft(client: GraphClient, uid: str) -> None:
    tag = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    resp = client.call(
        "POST",
        f"/users/{uid}/messages",
        json={
            "subject": f"E5 keepalive {tag}",
            "body": {"contentType": "Text", "content": "auto generated, safe to delete."},
            "toRecipients": [],
        },
    )
    if resp is not None and resp.ok:
        mid = (resp.json() or {}).get("id")
        if mid:
            client.call("DELETE", f"/users/{uid}/messages/{mid}")


def write_event(client: GraphClient, uid: str) -> None:
    start = datetime.now(timezone.utc) + timedelta(days=random.randint(10, 60))
    end = start + timedelta(minutes=15)
    resp = client.call(
        "POST",
        f"/users/{uid}/events",
        json={
            "subject": "E5 keepalive",
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "isReminderOn": False,
        },
    )
    if resp is not None and resp.ok:
        eid = (resp.json() or {}).get("id")
        if eid:
            client.call("DELETE", f"/users/{uid}/events/{eid}")


def write_folder(client: GraphClient, uid: str) -> None:
    name = f"e5_keepalive_{random.randint(10000, 99999)}"
    resp = client.call(
        "POST",
        f"/users/{uid}/drive/root/children",
        json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "replace"},
    )
    if resp is not None and resp.ok:
        fid = (resp.json() or {}).get("id")
        if fid:
            client.call("DELETE", f"/users/{uid}/drive/items/{fid}")


# --------------------------------------------------------------------------- #
# 单账户处理
# --------------------------------------------------------------------------- #
def resolve_users(client: GraphClient, acc: Account) -> list[str]:
    if acc.users:
        ids = []
        bad = []
        for upn in acc.users:
            resp = client.call("GET", f"/users/{upn}?$select=id,userPrincipalName")
            if resp is not None and resp.ok:
                ids.append((resp.json() or {}).get("id", ""))
            else:
                bad.append(upn)
                log.warning("[%s] 找不到用户 %s", acc.name, upn)
        if bad:
            raise RuntimeError(f"以下 UPN 不存在或无权读取：{', '.join(bad)}")
        return ids
    resp = client.call("GET", "/users?$select=id&$top=25")
    if resp is not None and resp.ok:
        ids = [u["id"] for u in (resp.json() or {}).get("value", [])]
        if ids:
            return ids
        raise RuntimeError("用户列表为空（异常，请检查租户）")
    code = resp.status_code if resp is not None else "无响应"
    raise RuntimeError(
        f"获取用户列表失败（HTTP {code}）——"
        f"请检查 User.Read.All 是否为【应用程序权限】且已授予管理员同意"
    )


def process_account(acc: Account) -> dict:
    result = {
        "name": acc.name, "users": 0, "ok": 0, "fail": 0, "skip": 0,
        "error": "", "sub": "", "expiry": "",
    }
    log.info("—— 开始处理账户 [%s] ——", acc.name)
    try:
        client = GraphClient(TokenProvider(acc))
        users = resolve_users(client, acc)
        if not users:
            raise RuntimeError("未获取到任何用户（检查权限/管理员同意/UPN）")
        result["users"] = len(users)

        # 订阅状态与到期时间（静默查询，不影响统计）
        sub, expiry = get_subscription_info(acc, client.tokens, users[0])
        result["sub"], result["expiry"] = sub, expiry
        if sub:
            log.info("[%s] 订阅状态: %s", acc.name, sub)
        if expiry:
            log.info("[%s] 到期时间: %s", acc.name, expiry)

        candidates: list[tuple[str, str]] = []
        for uid in users:
            endpoints = read_endpoints(uid)
            random.shuffle(endpoints)
            candidates.extend(endpoints[:4])
            if acc.enable_writes:
                candidates.append((random.choice(["DRAFT", "EVENT", "FOLDER"]), uid))
        random.shuffle(candidates)

        budget = random.randint(acc.min_calls, acc.max_calls)
        selected = candidates[:budget]
        log.info(
            "[%s] 计划 %d 个动作（预算 %d，候选 %d）",
            acc.name, len(selected), budget, len(candidates),
        )

        for i, job in enumerate(selected, 1):
            kind = job[0]
            if kind == "GET":
                client.call("GET", job[1])
            elif kind == "DRAFT":
                write_draft(client, job[1])
            elif kind == "EVENT":
                write_event(client, job[1])
            elif kind == "FOLDER":
                write_folder(client, job[1])
            if i < len(selected):
                time.sleep(random.uniform(0.6, 2.8))

        result.update(ok=client.ok, fail=client.fail, skip=client.skip)
    except Exception as exc:  # 单个账户出错不影响其他账户
        log.error("[%s] 处理失败：%s", acc.name, exc)
        result["error"] = str(exc)
    return result


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
def send_notification(title: str, body: str) -> None:
    """body 为 Markdown；各渠道任选，留空即不启用。失败只警告，不影响任务结果。"""
    plain = body.replace("**", "")

    key = os.getenv("NOTIFY_SERVERCHAN_KEY", "").strip()
    if key:
        try:
            r = requests.post(
                f"https://sctapi.ftqq.com/{key}.send",
                data={"title": title, "desp": body},
                timeout=30,
            )
            log.info("Server酱推送: %s", "成功" if r.ok else f"失败 {r.status_code}")
        except Exception as exc:
            log.warning("Server酱推送异常: %s", exc)

    tg_token = os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                data={"chat_id": tg_chat, "text": f"{title}\n\n{plain}"},
                timeout=30,
            )
            log.info("Telegram 推送: %s", "成功" if r.ok else f"失败 {r.status_code}")
        except Exception as exc:
            log.warning("Telegram 推送异常: %s", exc)

    smtp_host = os.getenv("NOTIFY_SMTP_HOST", "").strip()
    if smtp_host:
        try:
            port = _get_int("NOTIFY_SMTP_PORT", 465)
            user = os.getenv("NOTIFY_SMTP_USER", "")
            pwd = os.getenv("NOTIFY_SMTP_PASS", "")
            to = os.getenv("NOTIFY_SMTP_TO", "").strip() or user
            sender = os.getenv("NOTIFY_SMTP_FROM", "").strip() or user
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = sender
            msg["To"] = to
            msg.set_content(plain)
            if port == 465:
                with smtplib.SMTP_SSL(smtp_host, port, timeout=30) as s:
                    s.login(user, pwd)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, port, timeout=30) as s:
                    s.starttls()
                    s.login(user, pwd)
                    s.send_message(msg)
            log.info("邮件通知已发送给 %s", to)
        except Exception as exc:
            log.warning("邮件通知异常: %s", exc)


def write_step_summary(md: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    accounts = load_accounts()
    if not accounts:
        log.error("没有任何可用账户，请检查 Secrets 配置")
        return 2
    log.info("共 %d 个 E5 账户", len(accounts))

    started = time.time()
    results = [process_account(acc) for acc in accounts]
    duration = int(time.time() - started)

    total_ok = sum(r["ok"] for r in results)
    total_fail = sum(r["fail"] for r in results)
    total_skip = sum(r["skip"] for r in results)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"E5 KeepAlive 汇总：成功 {total_ok} / 失败 {total_fail} / 跳过 {total_skip}"

    lines = []
    for r in results:
        if r["error"]:
            lines.append(f"- **{r['name']}**：出错 —— {r['error']}")
        else:
            seg = (
                f"- **{r['name']}**：用户 {r['users']} 个，成功 {r['ok']}，"
                f"失败 {r['fail']}，跳过 {r['skip']}"
            )
            if r.get("sub"):
                seg += f" ｜ {r['sub']}"
            if r.get("expiry"):
                seg += f" ｜ 到期 {r['expiry']}"
            lines.append(seg)
    body = f"运行时间 {now_str}，耗时 {duration}s\n\n" + "\n".join(lines)

    write_step_summary("## " + title + "\n\n" + body + "\n")

    notify_on = os.getenv("NOTIFY_ENABLED", "true").strip().lower() != "false"
    if notify_on:
        send_notification(title, body)
    else:
        log.info("本次为上午场，按设置不推送（每天仅下午场运行后推送）")

    log.info("执行完毕: %s", title)
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::notice title=E5 KeepAlive::{title}")
    return 0 if total_ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
