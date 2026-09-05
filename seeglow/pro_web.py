# -*- coding: utf-8 -*-
"""拾光 SeeGlow · 网站版授权与计费层（Modal 部署用）

模式：服务器内置官方 API（访客无需自备 Key），但需赞助才能使用：
  通道一：爱发电订单号在线验证（¥29.9/月档），按购买月数签发票据
  通道二：作者手工发激活码（base36: 到期日+指纹+HMAC签名），可不赞助直接激活

设备绑定：每份授权最多绑定 3 台设备（机器码=浏览器指纹哈希）。
访客凭据保存在各自浏览器 localStorage；服务端不存明文 Key。

安全核心：AUTH_SECRET 只存在于 Modal 环境变量里，绝不能进开源仓库。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException, Request
from pydantic import BaseModel

# ---------------- 配置 ----------------

PLAN_PRICE = float(os.getenv("SEEGLOW_PLAN_PRICE", "29.9"))   # 月度档实付门槛
PLAN_DAYS = int(os.getenv("SEEGLOW_PLAN_DAYS", "35"))          # 每月折算天数
CODE_PLANS = {"month": PLAN_DAYS, "year": PLAN_DAYS * 12 + 15}
LIFETIME_CODE_EXPIRY = "9999-12-31"   # 永久码：到期日取极大值，实际等效永久

_verify_store = None   # modal.Dict：订单→设备绑定、激活码→绑定、限流


def init_store(store):
    global _verify_store
    _verify_store = store


def get_auth_secret() -> str:
    s = os.getenv("SEEGLOW_AUTH_SECRET", "")
    if not s:
        raise RuntimeError("SEEGLOW_AUTH_SECRET 未配置")
    return s


# ---------------- 机器码（浏览器指纹，前端传字符串即可） ----------------

def normalize_device(fp: str) -> str:
    """兼容保留：把设备标识转成稳定哈希（票据已不绑设备，仅个别场景用）。"""
    raw = (fp or "").strip().lower()
    if not raw or len(raw) > 300:
        raise HTTPException(400, "设备标识缺失")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------- 授权票据（浏览器本地保存的 JSON） ----------------

def sign_payload(payload: str) -> str:
    return hmac.new(get_auth_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_ticket(user_id: str, expiry: str, kind: str) -> dict:
    """user_id：订单号或激活码序号。票据自带 HMAC，客户端可离线判读到期日。"""
    body = f"{kind}|{user_id}|{expiry}"
    return {"kind": kind, "uid": user_id, "expiry": expiry, "sig": sign_payload(body)}


def verify_ticket(ticket: dict) -> dict:
    """校验本地票据（不绑设备）。合法返回 {"ok":True,...}，非法抛 HTTPException(401)。"""
    if not isinstance(ticket, dict):
        raise HTTPException(401, "无效的授权信息")
    uid = str(ticket.get("uid") or "")
    exp = str(ticket.get("expiry") or "")
    kind = str(ticket.get("kind") or "")
    if not (uid and exp and kind):
        raise HTTPException(401, "无效的授权信息")
    if not hmac.compare_digest(str(ticket.get("sig") or ""), sign_payload(f"{kind}|{uid}|{exp}")):
        raise HTTPException(401, "授权校验失败，请重新激活")
    try:
        d = datetime.strptime(exp, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(401, "授权数据损坏，请重新激活")
    if d < date.today():
        raise HTTPException(402, f"订阅已于 {exp} 到期，请续费后重新验证")
    return {"uid": uid, "expiry": exp, "kind": kind}


def _store_get(key: str, default=None):
    if _verify_store is None:
        return _local_store.get(key, default)
    try:
        v = _verify_store.get(key)
        return default if v is None else v
    except Exception:
        return _local_store.get(key, default)


def _store_put(key: str, value):
    # 无外部 KV（本地测试）时退化为进程内字典，保证撤销名单/限频逻辑可测
    if _verify_store is None:
        _local_store[key] = value
        return
    try:
        _verify_store.put(key, value)
    except Exception:
        pass


_local_store: dict = {}


# ---------------- 通道一：爱发电订单在线验证 ----------------

AFDIAN_USER_ID = os.getenv("AFDIAN_USER_ID", "")
AFDIAN_TOKEN = os.getenv("AFDIAN_TOKEN", "")


def _afidian_sign(params: str, ts: str) -> str:
    import hashlib as _h

    kv = f"params{params}ts{ts}user_id{AFDIAN_USER_ID}"
    return _h.md5((AFDIAN_TOKEN + kv).encode()).hexdigest()


def query_order(order_no: str) -> dict | None:
    """调爱发电开放 API 查订单；未配置凭证/网络失败返回 None（走人工兜底提示）。"""
    if not (AFDIAN_USER_ID and AFDIAN_TOKEN):
        return None
    import requests

    params = json.dumps({"out_trade_no": order_no})
    ts = str(int(time.time()))
    try:
        r = requests.post(
            "https://afdian.com/api/open/query-order",
            json={"user_id": AFDIAN_USER_ID, "params": params, "ts": ts,
                  "sign": _afidian_sign(params, ts)},
            timeout=15,
        )
        data = ((r.json() or {}).get("data") or {})
        lst = data.get("list") or []
        return lst[0] if lst else None
    except Exception:
        return None


def order_amount_yuan(o: dict) -> float:
    raw = o.get("show_amount") or o.get("amount") or 0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val / 100.0 if float(val).is_integer() and val >= 100 else val


class OrderReq(BaseModel):
    order_no: str
    device: str   # 浏览器设备标识


class CodeReq(BaseModel):
    code: str
    device: str


# ---------------- 激活码校验（作者离线签发，可不赞助直接激活） ----------------
# 设计：激活码不绑定设备——同一码可在任意多台设备激活（手机/电脑通吃），
# 签名只覆盖「到期日+码序号」，服务端仅做限频与撤销名单（modal.Dict）。

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 全局激活限频：每分钟最多 N 次（防穷举签名空间）
_CODE_ACTIVATE_RATE = 10


def _code_sig(expiry: str, serial: str) -> str:
    return hmac.new(get_auth_secret().encode(), f"webcode|{expiry}|{serial}".encode(),
                    hashlib.sha256).hexdigest()[:10]


def make_web_code(plan: str = "month") -> str:
    """生成一张不绑定设备的激活码。

    码结构：base36( 到期日YYYYMMDD + 随机序号8hex + HMAC签名10hex )
    序号随机生成（不来自机器码），所以同一张码谁拿到都能用、多设备通用；
    防伪造靠 HMAC；防滥用靠服务端撤销名单 + 限频。
    """
    days = CODE_PLANS.get(plan, PLAN_DAYS)
    expiry = (date.today() + timedelta(days=days)).isoformat()
    serial = uuid.uuid4().hex[:8]
    payload = expiry.replace("-", "") + serial + _code_sig(expiry, serial)  # 8+8+10 hex
    code = ""
    n = int(payload, 16)
    while n:
        n, r = divmod(n, 36)
        code = _B36[r] + code
    return "-".join(code[i : i + 5] for i in range(0, len(code), 5))


def parse_web_code(code: str) -> tuple[str, str]:
    """验签激活码。合法返回 (到期日ISO, 序号)，否则抛 HTTPException。"""
    s = re.sub(r"[^0-9A-Za-z]", "", (code or "").strip()).upper()
    if not (18 <= len(s) <= 26):
        raise HTTPException(400, "激活码格式不正确（请完整复制，含连字符）")
    try:
        raw = int(s, 36)
        hexstr = format(raw, "x").zfill(26)
        y, m, d = int(hexstr[0:4]), int(hexstr[4:6]), int(hexstr[6:8])
        expiry = f"{y:04d}-{m:02d}-{d:02d}"
        datetime.strptime(expiry, "%Y-%m-%d")
        serial, sig = hexstr[8:16], hexstr[16:26]
    except (ValueError, IndexError):
        raise HTTPException(400, "激活码无法识别，请核对后重新粘贴")
    if not hmac.compare_digest(sig, _code_sig(expiry, serial)):
        raise HTTPException(401, "激活码无效（签名校验失败）")
    if datetime.strptime(expiry, "%Y-%m-%d").date() < date.today():
        raise HTTPException(402, f"激活码已于 {expiry} 过期")
    return expiry, serial


# ---------------- 授权状态存储（浏览器 localStorage 即数据库，服务端只验签） ----------------

def check_request_auth(request: Request) -> dict | None:
    """从请求头 X-SeeGlow-Auth 读票据并校验。未携带返回 None；携带但非法抛 401/402。"""
    raw = request.headers.get("x-seeglow-auth") or ""
    if not raw:
        return None
    try:
        ticket = json.loads(raw)
    except Exception:
        raise HTTPException(401, "授权信息损坏，请重新激活")
    info = verify_ticket(ticket)
    return info


# ---------------- 撤销名单（可选）：把泄露/退款的激活码作废 ----------------

def revoke_code(serial: str):
    """作废一张激活码（modal.Dict 持久）：不能再激活任何新设备；已激活的票据到期为止。"""
    _store_put("revoked:" + serial.upper(), True)


def is_code_revoked(serial: str) -> bool:
    return bool(_store_get("revoked:" + serial.upper()))


# ---------------- 激活码签发（作者侧） ----------------

def _plan_expiry(plan: str) -> str:
    """各档位的到期日。permanent（永久）取极大日期，校验逻辑无需特判。"""
    if plan in ("lifetime", "perm", "forever"):
        return LIFETIME_CODE_EXPIRY
    days = CODE_PLANS.get(plan, PLAN_DAYS)
    return (date.today() + timedelta(days=days)).isoformat()


def make_web_code(plan: str = "month") -> str:
    """生成一张不绑定设备的激活码（作者侧工具用）。

    码结构：base36( 到期日YYYYMMDD + 随机序号8hex + HMAC签名10hex )
    序号随机，同一张码可在任意多台设备激活；防伪造靠 HMAC，
    防滥用靠服务端撤销名单（revoke_code）与限频。
    plan="lifetime" 签发永久码。
    """
    expiry = _plan_expiry(plan)
    serial = uuid.uuid4().hex[:8]
    payload = expiry.replace("-", "") + serial + _code_sig(expiry, serial)  # 8+8+10 hex
    code = ""
    n = int(payload, 16)
    while n:
        n, r = divmod(n, 36)
        code = _B36[r] + code
    return "-".join(code[i : i + 5] for i in range(0, len(code), 5))


# ---------------- 用户系统（账号 + 会话 + 每日试用额度） ----------------

TRIAL_PER_DAY = int(os.getenv("SEEGLOW_TRIAL_PER_DAY", "3"))
SESSION_DAYS = 30


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 100_000).hex()


def register_user(username: str, password: str) -> str:
    """注册新用户（用户名不区分大小写，唯一）。成功返回规范化用户名。"""
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]{2,24}", username):
        raise ValueError("用户名需 2~24 位，仅限中文、字母、数字、下划线")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    key = "user:" + username.lower()
    if _store_get(key):
        raise ValueError("该用户名已被注册")
    salt = os.urandom(16).hex()
    _store_put(key, {"salt": salt, "pw": _hash_password(password, salt),
                     "created": time.time(), "expiry": "", "plan": ""})
    return username.lower()


def verify_login(username: str, password: str) -> bool:
    rec = _store_get("user:" + (username or "").strip().lower())
    if not rec:
        return False
    return hmac.compare_digest(_hash_password(password or "", rec["salt"]), rec.get("pw", ""))


def issue_session(username: str) -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex[:16]
    _store_put("sess:" + token, {"user": username.lower(), "exp": time.time() + SESSION_DAYS * 86400})
    return token


def drop_session(token: str):
    if token:
        _store_put("sess:" + token, None)


def session_user(token: str) -> str | None:
    if not token:
        return None
    s = _store_get("sess:" + token)
    if not s or not isinstance(s, dict) or s.get("exp", 0) < time.time():
        return None
    return s.get("user")


def get_user(username: str) -> dict:
    return _store_get("user:" + (username or "")) or {}


def set_user_licence(username: str, plan: str, expiry: str):
    rec = get_user(username)
    if not rec:
        return
    rec["plan"], rec["expiry"] = plan, expiry
    _store_put("user:" + username, rec)


def user_licence_expiry(username: str) -> str:
    """会员有效期；非会员返回空串。"""
    rec = get_user(username)
    exp = str(rec.get("expiry") or "")
    try:
        if exp and datetime.strptime(exp, "%Y-%m-%d").date() >= date.today():
            return exp
    except ValueError:
        pass
    return ""


def _quota_key(username: str) -> str:
    return f"quota:{username}:{date.today().isoformat()}"


def quota_left(username: str) -> int:
    used = int(_store_get(_quota_key(username), 0) or 0)
    return max(TRIAL_PER_DAY - used, 0)


def consume_quota(username: str) -> int:
    """消耗一次当日体验额度。

    返回本次消耗后的剩余次数（0 表示"这是最后一次"）；
    额度已耗尽返回 -1（本次未消耗，调用方应拒绝）。
    语义：TRIAL_PER_DAY=3 表示当天最多成功跑 3 次。
    """
    k = _quota_key(username)
    used = int(_store_get(k, 0) or 0)
    if used >= TRIAL_PER_DAY:
        return -1
    used += 1
    _store_put(k, used)
    return TRIAL_PER_DAY - used
