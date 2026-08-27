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
MAX_MACHINES = int(os.getenv("SEEGLOW_MAX_MACHINES", "3"))     # 单授权设备上限
CODE_PLANS = {"month": PLAN_DAYS, "year": PLAN_DAYS * 12 + 15}

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


def verify_ticket(ticket: dict, device_hash: str) -> dict:
    """校验本地票据。合法返回 {"ok":True,...}，非法抛 HTTPException(401)。"""
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
    if kind == "afdian":
        recs = _bound_devices("bind:" + uid)
        if device_hash and device_hash not in recs:
            if len(recs) >= MAX_MACHINES:
                raise HTTPException(402, "该订阅已绑定 3 台设备上限，如需更换设备请联系作者")
            _remember_device("bind:" + uid, device_hash)
    elif kind == "code":
        pass  # 设备绑定在激活时一次性完成（codeused:），票据期内换设备直接拒绝
    return {"uid": uid, "expiry": exp, "kind": kind}


def _store_get(key: str, default=None):
    if _verify_store is None:
        return default
    try:
        v = _verify_store.get(key)
        return default if v is None else v
    except Exception:
        return default


def _store_put(key: str, value):
    if _verify_store is not None:
        try:
            _verify_store.put(key, value)
        except Exception:
            pass


def _bound_devices(key: str) -> list:
    return list(_store_get(key) or [])


def _remember_device(key: str, device_hash: str):
    bound = _bound_devices(key)
    bound.append(device_hash)
    _store_put(key, bound)


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

_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _code_fingerprint(device_hash: str) -> str:
    """机器码指纹（掺 SECRET 防直接构造）。"""
    return hashlib.sha256((get_auth_secret() + device_hash).encode()).hexdigest()[:8]


def _code_sig(expiry: str, fp: str) -> str:
    return hmac.new(get_auth_secret().encode(), f"webcode|{expiry}|{fp}".encode(),
                    hashlib.sha256).hexdigest()[:10]


def parse_web_code(code: str, device_hash: str) -> str:
    """校验网站版激活码，合法返回到期日 ISO 字符串，否则抛 HTTPException。"""
    s = re.sub(r"[^0-9A-Za-z]", "", (code or "").strip()).upper()
    if not (18 <= len(s) <= 26):
        raise HTTPException(400, "激活码格式不正确（请完整复制，含连字符）")
    try:
        raw = int(s, 36)
        hexstr = format(raw, "x").zfill(26)
        y, m, d = int(hexstr[0:4]), int(hexstr[4:6]), int(hexstr[6:8])
        expiry = f"{y:04d}-{m:02d}-{d:02d}"
        datetime.strptime(expiry, "%Y-%m-%d")
        fp, sig = hexstr[8:16], hexstr[16:26]
    except (ValueError, IndexError):
        raise HTTPException(400, "激活码无法识别，请核对后重新粘贴")
    if not hmac.compare_digest(sig, _code_sig(expiry, fp)):
        raise HTTPException(401, "激活码无效（签名校验失败）")
    if not hmac.compare_digest(fp, _code_fingerprint(device_hash)):
        raise HTTPException(401, "此激活码绑定在其他设备，请提供本机机器码重新获取")
    if datetime.strptime(expiry, "%Y-%m-%d").date() < date.today():
        raise HTTPException(402, f"激活码已于 {expiry} 过期")
    # 一码限设备：绑定满 3 台后拒绝新设备（与订阅同策略）
    uid = "CODE-" + hashlib.sha256(fp.encode()).hexdigest()[:12].upper()
    used = _bound_devices("codeused:" + uid)
    if device_hash not in used:
        if len(used) >= MAX_MACHINES:
            raise HTTPException(402, "该激活码已绑定 3 台设备上限，如需更换设备请联系作者")
        _remember_device("codeused:" + uid, device_hash)
    return expiry


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
    device = normalize_device(request.headers.get("x-seeglow-device", ""))
    info = verify_ticket(ticket, device)
    return {**info, "device": device}


# ---------------- 激活码签发（作者侧） ----------------

def make_web_code(device_fingerprint: str, plan: str = "month") -> str:
    """为设备标识（前端显示的机器码原文或其哈希）生成网站版激活码。

    码结构：base36( 到期日YYYYMMDD + 设备指纹8hex + HMAC签名10hex )
    """
    days = CODE_PLANS.get(plan, PLAN_DAYS)
    expiry = (date.today() + timedelta(days=days)).isoformat()
    dhash = normalize_device(device_fingerprint)
    fp = _code_fingerprint(dhash)
    payload = expiry.replace("-", "") + fp + _code_sig(expiry, fp)  # 8+8+10 hex
    code = ""
    n = int(payload, 16)
    while n:
        n, r = divmod(n, 36)
        code = _B36[r] + code
    return "-".join(code[i : i + 5] for i in range(0, len(code), 5))
