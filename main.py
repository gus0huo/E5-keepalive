#!/usr/bin/env python3
"""
E5 KeepAlive — AutoApiSecret 风格的 E5 开发活跃度维持脚本
- OAuth 2.0 client_credentials 流程，凭证仅来自 GitHub Secrets
- 可通过 TARGET_USERS 变量指定只跑哪几个账号
- 写操作（草稿/日历/文件夹）创建后立即删除，不留残留
- 429/5xx 指数退避；404/507（对方账号问题）直接跳过，不重试
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

log = logging.getLogger("e5-keepalive")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self) -> None:
        self.tenant = os.getenv("AZURE_TENANT_ID") or "organizations"
        self.client_id = os.getenv("AZURE_CLIENT_ID", "")
        self.client_secret = os.getenv("AZURE_CLIENT_SECRET", "")
        self.target_users = [
            u.strip() for u in os.getenv("TARGET_USERS", "").split(",") if u.strip()
        ]
        self.min_calls = int(os.getenv("MIN_CALLS", "18"))
        self.max_calls = int(os.getenv("MAX_CALLS", "36"))
        self.enable_writes = os.getenv("ENABLE_WRITES", "true").lower() != "false"

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("AZURE_CLIENT_ID", self.client_id),
                ("AZURE_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            log.error("缺少 Secrets: %s", ", ".join(missing))
            sys.exit(2)
        if self.min_calls > self.max_calls:
            self.min_calls, self.max_calls = self.max_calls, self.min_calls


# --------------------------------------------------------------------------- #
# Token 管理
# --------------------------------------------------------------------------- #
class TokenProvider:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._token = ""
        self._expires_at = 0.0

    def invalidate(self) -> None:
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token
        url = TOKEN_ENDPOINT.format(tenant=self.cfg.tenant)
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            log.error("获取 token 失败: %s %s", resp.status_code, resp.text[:300])
            sys.exit(3)
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        log.info("已获取 access token (有效期 %ss)", payload.get("expires_in"))
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
            # 404（未开通 OneDrive）、507（存储已满）重试无意义，直接跳过
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
        ("GET", f"/users/{uid}/contacts?$top=5&$select=id,displayName"),
        ("GET", f"/users/{uid}/drive?$select=id,driveType,quota"),
        ("GET", f"/users/{uid}/drive/root/children?$top=5&$select=id,name,size"),
        ("GET", f"/users/{uid}/photo/$value"),
        ("GET", f"/users/{uid}/memberOf?$top=5&$select=id,displayName"),
        ("GET", f"/users/{uid}/onenote/notebooks?$select=id,displayName"),
        ("GET", f"/users/{uid}/chats?$top=5&$select=id,topic"),
    ]


# --------------------------------------------------------------------------- #
# 可回滚的写操作：创建后立即删除
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
# 主流程
# --------------------------------------------------------------------------- #
def resolve_users(client: GraphClient, cfg: Config) -> list[str]:
    if cfg.target_users:
        ids = []
        for upn in cfg.target_users:
            resp = client.call("GET", f"/users/{upn}?$select=id,userPrincipalName")
            if resp is not None and resp.ok:
                ids.append((resp.json() or {}).get("id", ""))
            else:
                log.warning("找不到用户 %s，请检查 UPN 是否写对", upn)
        return [i for i in ids if i]
    resp = client.call("GET", "/users?$select=id&$top=25")
    if resp is None or not resp.ok:
        return []
    return [u["id"] for u in (resp.json() or {}).get("value", [])]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config()
    cfg.validate()

    client = GraphClient(TokenProvider(cfg))
    users = resolve_users(client, cfg)
    if not users:
        log.error("未获取到任何用户，请检查 User.Read.All 权限、管理员同意、TARGET_USERS 写法")
        return 1
    log.info("目标用户数: %d", len(users))

    # 每个用户：4 个随机只读 + 1 个随机写，再按预算抽样
    candidates: list[tuple[str, str]] = []
    for uid in users:
        endpoints = read_endpoints(uid)
        random.shuffle(endpoints)
        candidates.extend(endpoints[:4])
        if cfg.enable_writes:
            candidates.append((random.choice(["DRAFT", "EVENT", "FOLDER"]), uid))
    random.shuffle(candidates)

    budget = random.randint(cfg.min_calls, cfg.max_calls)
    selected = candidates[:budget]
    log.info(
        "本次计划执行 %d 个动作（预算 %d，候选 %d）",
        len(selected), budget, len(candidates),
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

    log.info("执行完毕: 成功 %d，失败 %d，跳过 %d", client.ok, client.fail, client.skip)
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(
            f"::notice title=E5 KeepAlive::成功 {client.ok} 次，"
            f"失败 {client.fail} 次，跳过 {client.skip} 次"
        )
    return 0 if client.ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
