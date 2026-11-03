#!/usr/bin/env python3
"""
E5 KeepAlive — AutoApiSecret 风格的 E5 开发活跃度维持脚本
- 多 E5 账户：secret E5_ACCOUNTS（JSON 数组），单账户模式向后兼容
- 预算模型：每账户每天恰好 N 次请求；N 来自 schedule.yml 的 plan_min~plan_max，每天抽一次
- 波次由 schedule.yml 定义：每波执行当天总量的指定百分比，末波跑完剩余并推送
- 账本按北京日期跨波次累计（含每波运行流水），持久化在 .e5_state.json；崩溃时已执行次数也会记账
- 推送为表格版：账户 / 用户 / 今日请求 / 各波 时间(请求数) / 到期时间
- 网络超时/连接异常自动退避重试，不影响同账户后续动作
- 运行结果通知：GitHub Step Summary + Server酱 / Telegram / SMTP 邮件（任选）
- 日志脱敏：UPN / GUID / 密码自动打码（REDACT_LOGS=false 可关闭）
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
STATE_FILE = os.getenv("STATE_FILE", ".e5_state.json")

log = logging.getLogger("e5-keepalive")


# --------------------------------------------------------------------------- #
# 日志脱敏（开源发布时隐藏 UPN / GUID / 密码）
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SECRET_RE = re.compile(r"(?i)(\"[^\"]*secret[^\"]*\"\s*:\s*\")[^\"]*")


def _mask_email(m: re.Match) -> str:
    name, _, domain = m.group(0).partition("@")
    return f"{name[:2]}***@{domain[:1]}***"


def redact_text(msg: str) -> str:
    msg = _EMAIL_RE.sub(_mask_email, msg)
    msg = _GUID_RE.sub(lambda m: m.group(0)[:4] + "***", msg)
    msg = _SECRET_RE.sub(r"\1***", msg)
    return msg


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def _beijing_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _today_key() -> str:
    """北京日期（UTC+8）——账本统一按北京时间的'天'记账。"""
    return _beijing_now().strftime("%Y-%m-%d")


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
    daily_min: int = 70          # 每天总请求数下限（来自 schedule.yml 的 plan_min）
    daily_max: int = 90          # 每天总请求数上限（来自 schedule.yml 的 plan_max）
    runs_per_day: int = 3        # 每天跑几波（仅用于日志参考）
    min_calls: int = 18          # 旧随机模式用，daily 为 0 时生效
    max_calls: int = 36
    enable_writes: bool = True
    expiry: str = ""             # 手动指定到期日，如 "2027-01-20"
    expiry_scan_user: str = ""   # 扫描哪个邮箱的微软通知邮件（建议填管理员 UPN）


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _parse_calls(value, default: tuple[int, int] = (70, 90)) -> tuple[int, int]:
    """解析请求数：80 → (80, 80)；"70-90" → (70, 90)；非法值回退默认。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        v = int(value)
        return v, v
    s = str(value).strip()
    if not s:
        return default
    m = re.match(r"^(\d+)\s*[-~—]\s*(\d+)$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, b) if a <= b else (b, a)
    if s.isdigit():
        v = int(s)
        return v, v
    return default


def load_accounts() -> list[Account]:
    """优先读 E5_ACCOUNTS(JSON)；否则退回单账户三件套环境变量。"""
    raw = os.getenv("E5_ACCOUNTS", "").strip()
    env_calls = _parse_calls(os.getenv("DAILY_CALLS", "").strip() or None)
    env_runs = _get_int("RUNS_PER_DAY", 3)
    default_min = _get_int("MIN_CALLS", 18)
    default_max = _get_int("MAX_CALLS", 36)
    default_writes = os.getenv("ENABLE_WRITES", "true").strip().lower() != "false"

    accounts: list[Account] = []
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("E5_ACCOUNTS 不是合法 JSON：%s", exc)
            raw_lines = raw.splitlines()
            for i in range(max(1, exc.lineno - 1), min(len(raw_lines), exc.lineno + 1) + 1):
                marker = " >>>" if i == exc.lineno else "    "
                log.error("%s 第 %d 行: %s", marker, i, raw_lines[i - 1])
            sys.exit(2)
        for i, it in enumerate(items, 1):
            if "daily_calls" in it:
                log.info(
                    "[%s] daily_calls 字段已失效，每天总量统一由 schedule.yml 管理",
                    it.get("name") or f"账户{i}",
                )
            dmin, dmax = env_calls   # 每天总量区间来自 schedule.yml（DAILY_CALLS 环境变量）
            accounts.append(
                Account(
                    name=it.get("name") or f"账户{i}",
                    tenant_id=it.get("tenant_id", "") or "organizations",
                    client_id=it.get("client_id", ""),
                    client_secret=it.get("client_secret", ""),
                    users=[u.strip() for u in it.get("users", []) if u.strip()],
                    daily_min=dmin,
                    daily_max=dmax,
                    runs_per_day=int(it.get("runs_per_day", env_runs)),
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
                daily_min=env_calls[0],
                daily_max=env_calls[1],
                runs_per_day=env_runs,
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
            if acc.runs_per_day < 1:
                acc.runs_per_day = 1
            valid.append(acc)
        else:
            log.warning("账户 %s 缺少 client_id/client_secret，已跳过", acc.name)
    return valid


# --------------------------------------------------------------------------- #
# 每日记账（按北京日期：当日目标 + 累计用量 + 每波运行流水）
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    today = _today_key()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "usage": {}, "targets": {}, "history": {}}
    state.setdefault("usage", {})
    state.setdefault("targets", {})
    state.setdefault("history", {})
    return state


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError as exc:
        log.warning("状态文件写入失败（不影响本次运行）: %s", exc)


def record_history(state: dict, name: str, req_count: int) -> None:
    """记录一波运行：时间(北京 HH:MM) + 请求数，最多保留最近 12 条。"""
    hist = state["history"].setdefault(name, [])
    hist.append({"t": _beijing_now().strftime("%H:%M"), "n": int(req_count)})
    if len(hist) > 12:
        del hist[:-12]


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
        try:
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
        except requests.RequestException as exc:
            raise RuntimeError(f"获取 token 网络异常: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            raise RuntimeError(f"获取 token 失败: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        log.info("[%s] 已获取 access token", self.acc.name)
        return self._token


# --------------------------------------------------------------------------- #
# Graph 客户端（重试 + 统计；超时/网络异常同样退避重试）
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
            try:
                resp = self.http.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {self.tokens.get()}"},
                    timeout=30,
                    **kwargs,
                )
            except requests.RequestException as exc:
                # 超时 / 连接失败等网络问题：退避后重试，绝不让单次抖动中断账户
                wait = 2 ** attempt
                log.warning(
                    "%s %s 网络异常（%s），%ss 后重试",
                    method, path, exc.__class__.__name__, wait,
                )
                time.sleep(wait)
                continue
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
        log.info("订阅信息查询异常，已忽略: %s", exc.__class__.__name__)
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
    today = _today_key()

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

    # 2) 到期时间：手动配置 > 邮件推算（只认最近 120 天的邮件和未来的日期）
    expiry = ""
    if acc.expiry:
        expiry = f"{acc.expiry}（手动配置）"
    else:
        uid = acc.expiry_scan_user or fallback_uid
        if uid:
            since = (datetime.now(timezone.utc) - timedelta(days=120)).strftime(
                "%Y-%m-%dT00:00:00Z"
            )
            kw = "expires OR expiring OR expiration OR renewal OR renewed OR 续订 OR 续期 OR 到期"
            data = _plain_get(
                tokens,
                f"/users/{uid}/messages?$search=\"{kw}\""
                f"&$filter=receivedDateTime ge {since}&$top=10"
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
                    if d and d >= today:  # 过去的日期没有意义，丢弃
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
# 预算分摊与单账户处理
# --------------------------------------------------------------------------- #
def plan_user_jobs(uid: str, budget: int, enable_writes: bool) -> tuple[list, int]:
    """把 budget 个请求分给单个用户。GET=1 次，写操作=2 次。"""
    jobs: list[tuple[str, str]] = []
    used = 0
    if budget <= 0:
        return jobs, used
    if enable_writes and budget >= 3:
        jobs.append((random.choice(["DRAFT", "EVENT", "FOLDER"]), uid))
        used += 2
    gets = read_endpoints(uid)
    random.shuffle(gets)
    i = 0
    while used < budget:
        if i >= len(gets):
            i = 0
            random.shuffle(gets)   # 额度超过接口数时允许重复随机取
        jobs.append(gets[i])
        used += 1
        i += 1
    return jobs, used


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


def process_account(acc: Account, usage: dict, targets: dict) -> dict:
    result = {
        "name": acc.name, "users": 0, "ok": 0, "fail": 0, "skip": 0,
        "error": "", "sub": "", "expiry": "",
        "req_run": 0, "req_target": 0, "req_used": 0,
    }
    log.info("—— 开始处理账户 [%s] ——", acc.name)
    done_req = 0   # 已实际执行的请求数（崩溃时用于部分记账）
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

        # ---- 预算：本波执行【当天总量】的 WAVE_PERCENT 百分比，末波跑完剩余 ----
        used_so_far = int(usage.get(acc.name, 0))
        window = os.getenv("RUN_WINDOW", "").strip().lower()
        is_last_env = os.getenv("IS_LAST", "").strip().lower()
        if is_last_env in ("true", "false"):
            is_last_wave = is_last_env == "true"
        else:
            is_last_wave = window in ("w3", "pm", "manual")  # 兼容旧版配置
        pct_spec = os.getenv("WAVE_PERCENT", "").strip().lower()

        if acc.daily_max > 0:
            target = int(targets.get(acc.name, 0))
            if not target:
                target = random.randint(acc.daily_min, acc.daily_max)
                targets[acc.name] = target
                log.info(
                    "[%s] 今日目标抽定：%d 次（范围 %d-%d）",
                    acc.name, target, acc.daily_min, acc.daily_max,
                )
            else:
                log.info("[%s] 今日目标沿用：%d 次", acc.name, target)
            result["req_target"] = target
            remaining = max(0, target - used_so_far)
            if remaining == 0:
                log.info(
                    "[%s] 今日预算已完成（%d/%d 次），本次不再发起活动请求",
                    acc.name, used_so_far, target,
                )
                result["req_used"] = used_so_far
                result.update(ok=client.ok, fail=client.fail, skip=client.skip)
                return result

            if is_last_wave or pct_spec == "rest":
                # 末波（或手测）：跑完剩余，保证每天恰好完成当天总量
                run_budget = remaining
                log.info(
                    "[%s] 末波：跑完剩余 %d 次（目标 %d，已用 %d）",
                    acc.name, remaining, target, used_so_far,
                )
            else:
                # 本波执行【当天总量】的 lo%~hi%（如 "30-50" 或 "40"），不超过剩余
                m = re.match(r"^(\d{1,3})(?:-(\d{1,3}))?$", pct_spec)
                if m:
                    lo_pct = int(m.group(1))
                    hi_pct = int(m.group(2) or m.group(1))
                else:
                    lo_pct, hi_pct = 30, 50
                lo = max(1, target * lo_pct // 100)
                hi = max(lo, target * hi_pct // 100)
                run_budget = min(remaining, random.randint(lo, hi))
                log.info(
                    "[%s] 本次跑 %d 个请求（目标 %d 的 %d~%d%%），剩余 %d 个留给后续波次",
                    acc.name, run_budget, target, lo_pct, hi_pct,
                    target - used_so_far - run_budget,
                )
        else:
            run_budget = random.randint(acc.min_calls, acc.max_calls)
            log.info("[%s] 随机预算模式：%d 次", acc.name, run_budget)

        base, remainder = divmod(run_budget, len(users))
        if base == 0:
            log.warning(
                "[%s] 每用户预算过低（%d），建议提高 plan 或减少 users",
                acc.name, base,
            )

        selected: list[tuple[str, str]] = []
        total_req = 0
        for idx, uid in enumerate(users):
            user_budget = base + (1 if idx < remainder else 0)
            jobs_u, used = plan_user_jobs(uid, user_budget, acc.enable_writes)
            selected.extend(jobs_u)
            total_req += used
        random.shuffle(selected)
        log.info(
            "[%s] 计划 %d 个动作，共 %d 个请求（%d 个用户，每人 %d~%d 次）",
            acc.name, len(selected), total_req, len(users),
            base + (1 if remainder else 0), base,
        )

        for i, job in enumerate(selected, 1):
            kind = job[0]
            if kind == "GET":
                client.call("GET", job[1])
                done_req += 1
            elif kind == "DRAFT":
                write_draft(client, job[1])
                done_req += 2
            elif kind == "EVENT":
                write_event(client, job[1])
                done_req += 2
            elif kind == "FOLDER":
                write_folder(client, job[1])
                done_req += 2
            if i < len(selected):
                time.sleep(random.uniform(0.6, 2.8))

        if acc.daily_max > 0:
            usage[acc.name] = used_so_far + total_req
            result["req_used"] = usage[acc.name]
        result["req_run"] = total_req
        result.update(ok=client.ok, fail=client.fail, skip=client.skip)
    except Exception as exc:  # 单个账户出错不影响其他账户
        log.error("[%s] 处理失败：%s", acc.name, exc)
        result["error"] = str(exc)
        # 中途出错也要把已执行的请求记账，避免下次补跑导致超额
        if done_req and acc.daily_max > 0:
            usage[acc.name] = int(usage.get(acc.name, 0)) + done_req
            result["req_used"] = usage[acc.name]
        result["req_run"] = done_req
    return result


# --------------------------------------------------------------------------- #
# 表格渲染（推送 / Summary 用 Markdown 表格，Telegram/邮件用纯文本行）
# --------------------------------------------------------------------------- #
def build_table(results: list[dict], history: dict) -> tuple[str, str]:
    wave_n = max((len(history.get(r["name"], [])) for r in results), default=0)
    wave_n = min(max(wave_n, 1), 6)   # 最多 6 列，防止手动测试太多把表撑爆

    headers = ["账户", "用户", "今日请求"] + [f"第{i}次" for i in range(1, wave_n + 1)] + ["到期时间"]
    md_lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    plain_lines = []

    for r in results:
        hist = history.get(r["name"], [])
        cells = []
        for i in range(wave_n):
            if i < len(hist):
                cells.append(f"{hist[i]['t']}（{hist[i]['n']}）")
            else:
                cells.append("—")
        used = f"{r['req_used']}/{r['req_target']}" if r["req_target"] > 0 else f"{r['req_run']} 次"
        row = [r["name"], str(r["users"]), used] + cells + [r.get("expiry") or "未查到"]
        md_lines.append("| " + " | ".join(row) + " |")
        plain_lines.append(" ｜ ".join(row))

    return "\n".join(md_lines), "\n".join(plain_lines)


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
def send_notification(title: str, body: str, plain: str | None = None) -> None:
    """body 为 Markdown（表格），plain 为纯文本版。各渠道任选，留空即不启用。"""
    if plain is None:
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
            log.warning("Server酱推送异常: %s", exc.__class__.__name__)

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
            log.warning("Telegram 推送异常: %s", exc.__class__.__name__)

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
            msg.set_content(f"{title}\n\n{plain}")
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
            log.warning("邮件通知异常: %s", exc.__class__.__name__)


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
    if os.getenv("REDACT_LOGS", "true").strip().lower() != "false":
        for h in logging.getLogger().handlers:
            h.addFilter(RedactFilter())

    accounts = load_accounts()
    if not accounts:
        log.error("没有任何可用账户，请检查 Secrets 配置")
        return 2
    log.info("共 %d 个 E5 账户", len(accounts))

    state = load_state()
    save_state(state)  # 先落盘，确保中途失败也有状态文件
    usage = state["usage"]
    targets = state["targets"]

    started = time.time()
    results = []
    for acc in accounts:
        r = process_account(acc, usage, targets)
        results.append(r)
        record_history(state, acc.name, r["req_run"])   # 记录本波运行流水
        save_state(state)                               # 每个账户跑完立即记账
    duration = int(time.time() - started)

    total_ok = sum(r["ok"] for r in results)
    total_fail = sum(r["fail"] for r in results)
    total_skip = sum(r["skip"] for r in results)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"E5 KeepAlive 汇总：成功 {total_ok} / 失败 {total_fail} / 跳过 {total_skip}"

    # ---- 表格 + 备注 ----
    md_table, plain_table = build_table(results, state["history"])

    notes_md, notes_plain = [], []
    for r in results:
        if r["error"]:
            msg = f"{r['name']} 出错：{r['error']}"
            if r["req_run"]:
                msg += f"（中断前已执行 {r['req_run']} 次，已记账）"
            notes_md.append(f"- {msg}")
            notes_plain.append(f"- {msg}")
        else:
            bits = []
            if r["fail"] or r["skip"]:
                bits.append(f"失败 {r['fail']}，跳过 {r['skip']}")
            if r.get("sub"):
                bits.append(r["sub"])
            if bits:
                notes_md.append(f"- {r['name']}：" + " ｜ ".join(bits))
                notes_plain.append(f"- {r['name']}：" + " ｜ ".join(bits))

    intro = f"运行时间 {now_str}，耗时 {duration}s"
    body_md = intro + "\n\n" + md_table
    body_plain = intro + "\n\n" + plain_table
    if notes_md:
        body_md += "\n\n" + "\n".join(notes_md)
        body_plain += "\n\n" + "\n".join(notes_plain)
    body_md = redact_text(body_md)
    body_plain = redact_text(body_plain)

    write_step_summary("## " + title + "\n\n" + body_md + "\n")

    notify_on = os.getenv("NOTIFY_ENABLED", "true").strip().lower() != "false"
    if notify_on:
        send_notification(title, body_md, body_plain)
    else:
        log.info("本次为非推送波次，按设置不推送（每天仅末波运行后推送）")

    log.info("执行完毕: %s", title)
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::notice title=E5 KeepAlive::{title}")
    return 0 if total_ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
