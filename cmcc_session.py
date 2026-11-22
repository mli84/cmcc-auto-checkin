#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动 App 会话签发（client.app.coc.10086.cn /biz-orange/LN/uamonekeylogin/autoLogin）
========================================================================================

用途：随时现签一张「新鲜活会话」cookie，供 `appTokenLogin` 使用，从而彻底摆脱
「app_token 空闲 1~3.5 小时即过期、需要反复抓包」的问题。

关键实测结论（2026-10-26，多组对照验证）：

  1. autoLogin **不校验 JSESSIONID**：随便给一个随机 UUID 甚至空值都能返回
     retCode=000000，并换发一张全新的活会话。它靠请求体里的 `cellNum`
     （RSA-1024 加密的手机号）识别用户。
  2. 因此**不需要任何常驻保活**。只要每次运行时现调一次 autoLogin 拿到新 cookie，
     再喂给 appTokenLogin 即可。过期问题的根因是 appTokenLogin 会在会话闲置后
     回收它，而 autoLogin 能无视该状态重新签发。
  3. 唯一前提是 `Cookie` 头必须存在（缺了报 `950102 x-sign错误`，因为 x-sign
     拿 JSESSIONID 拼串）。值本身无所谓。
  4. 同一号码约 1 分钟内重复 autoLogin 会返回 `233031` 限流；不同号码互不影响。
     频率再高会升级为行为风控：`214100`（请求疑似存在风险）、`214103`（登录过于
     频繁，需人脸识别解封）。**autoLogin 就是一次 App 一键登录**，因此本模块把会话
     落盘复用（`MINT_TTL`）并限制跨进程最短现签间隔（`MINT_MIN_INTERVAL`），命中
     风控码后还会静默一段时间（`RISK_SILENCE`）。

加密体系（与真实抓包逐字节对齐，见 --self-test）：

  请求体   AES-128-CBC(key=tVkdaRWRY0ZkV1Vr, iv=VjFSQ1ZtVkQxRTlQ) + PKCS7 → base64
  响应体   base64 → AES-128-CBC(key=RYV0hCV1lV25KYVJ, iv=VjFSQ1ZtVkQxRTlQ)
  x-token  base64(AES-128-CBC(key=bAIgvwAuA4tbDr9d, iv=9791027341711819)
                  (xk + "_" + path + "_" + x-time + "_" + x-nonce))
  xs       md5(url + "_" + 明文JSON + "_Leadeon/SecurityOrganization")
  x-sign   md5(x-token + "_" + x-time + "_" + x-nonce + "_" + 当前JSESSIONID值)

明文里的 `xk` 与 `reqBody.cellNum`（App 内自研 RSA 生成，无法伪造）必须沿用抓包值。
网关强校验 x-qen/xs/xk/cv（任一错误即 403）。

用法：
  python3 cmcc_session.py --har 抓包.har        # 解包存 profile（一次即可）
  python3 cmcc_session.py --refresh             # 现签一张新会话，打印 cookie
  python3 cmcc_session.py --refresh --config config.json   # 顺带回写 app_token
  python3 cmcc_session.py --show                # 查看当前会话剩余有效期
  python3 cmcc_session.py --self-test           # 离线自检（无需抓包）

其他脚本无需改动：cmcc_sign.exchange_session() 在 appTokenLogin 失败时会自动调用
本模块现签新会话后重试。

退出码：0 = 成功；1 = 失败（需重新抓包）
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import re
import ssl
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

ROOT = Path(__file__).resolve().parent

SALT = "Leadeon/SecurityOrganization"
REQ_KEY, REQ_IV = b"tVkdaRWRY0ZkV1Vr", b"VjFSQ1ZtVkQxRTlQ"
RESP_KEY = b"RYV0hCV1lV25KYVJ"
TOK_KEY, TOK_IV = b"bAIgvwAuA4tbDr9d", b"9791027341711819"
UA = "ChinaMobile/12.5.5 (iPhone; iOS 26.7.1; Scale/3.00)"
REFRESH_PATH = "/biz-orange/LN/uamonekeylogin/autoLogin"

# 服务端明确表示「本次登录不可继续」的返回码 → (原因, 处置建议)
DEAD_CODES = {
    "410000": ("会话空闲超时", "请重新冷启动抓包后用 --har 导入"),
    "233031": ("登录请求过于频繁（同号短时间多次续期，稍后重试）", "稍后重试即可"),
    "210000": ("会话已失效", "请重新冷启动抓包后用 --har 导入"),
    # 该号码在别的设备（通常是真机 App）登录过，jwt/app_token 被顶掉，
    # 且另一设备会话存活期间服务端拒绝再签发。实测与端点/模板字段无关，
    # 只与「模板里的设备指纹是否等于当前在用手机」有关：换机后必须重抓模板。
    "420000": ("该号码已在其他设备登录，服务端拒绝再签发会话",
               "在该号码当前手机上打开中国移动 App 退出登录后重试；若换过手机，"
               "用当前手机的抓包重新 --har 导入 profile（模板里的设备指纹须与在用手机一致）"),
    "214100": ("请求疑似存在风险，需完成验证",
               "完成 App 内人机验证后隔几小时再试；脚本已自动静默，期间不要再手动重跑"),
    "214103": ("账号近期登录过于频繁（行为风控）",
               "立刻停掉所有定时任务，在 App 内完成人脸识别解封，至少隔一天再试"),
    # 号码的一键登录凭证过期（通常发生在 App 退出登录/解封后）：重试无用，须在 App 里重新登录一次
    "213245": ("该手机号码的一键登录已过期",
               "在 App 内重新登录一次，然后清掉本地静默再运行"),
}

log = logging.getLogger("cmcc_session")

# 会话复用与现签冷却（跨进程）。autoLogin 就是一次「一键登录」：流量 Widget 每 15
# 分钟刷新一次，若每次都现签，一天就是几十次登录，账号很快被 214100/214103 风控
# （214103 需人脸识别解封）。故把会话落盘复用，并限制最短现签间隔。
_MINTED: dict[str, tuple[float, str]] = {}
MINT_TTL = 8 * 3600          # 会话复用窗口；Widget 每 15 分钟用一次不会闲置，失效时由调用方清缓存
MINT_MIN_INTERVAL = 600      # 两次现签的最小间隔（跨进程，防多脚本同时抢签）
RISK_SILENCE = {             # 命中风控码后的静默时长：继续登录只会把封控窗口拖长
    "233031": 600,           # 同号短时间多次续期，约 1 分钟窗口
    "214100": 2 * 3600,      # 请求疑似存在风险，需完成验证
    "214103": 24 * 3600,     # 登录过于频繁，需人脸识别解封
    "213245": 2 * 3600,      # 一键登录凭证过期，重试无用；App 重新登录后可手动清静默
}
DEAD_BACKOFF_AFTER = 3       # 业务侧连续这么多次判定会话失效后停止重签
DEAD_BACKOFF = 6 * 3600      # 退避时长：每次重签都是一次登录，持续重试只会拖长封控
# 最近一次自动登录失败信息（供调用方把真实原因拼进报错，避免误导用户去重新抓包）
LAST_ERROR: dict = {}


class LegacyAdapter(HTTPAdapter):
    """client.app.coc.10086.cn 要求 legacy renegotiation + 低安全等级套件。

    注意：只设 SECLEVEL=1 不够，缺 OP_LEGACY_SERVER_CONNECT 会直接
    SSLError: UNSAFE_LEGACY_RENEGOTIATION_DISABLED（实测）。
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# ---------------------------------------------------------------- 加密原语

def _cbc(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    if encrypt:
        pad = 16 - len(data) % 16
        enc = cipher.encryptor()
        return enc.update(data + bytes([pad]) * pad) + enc.finalize()
    dec = cipher.decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 padding 不合法")
    return out[:-pad]


def encrypt_body(plain: str) -> str:
    """明文 JSON → 请求体 base64（xs 用的是同一个明文字符串）。"""
    return base64.b64encode(_cbc(REQ_KEY, REQ_IV, plain.encode(), True)).decode()


def decrypt_response(raw: bytes) -> dict | None:
    """响应体 → dict。线上为 base64 文本；Proxyman HAR 是二次 base64。"""
    text = raw.decode("utf-8", "replace").strip()
    for _ in (1, 2):
        try:
            plain = _cbc(RESP_KEY, REQ_IV, base64.b64decode(text), False).decode()
        except Exception:
            return None
        if plain.startswith("{"):
            return json.loads(plain)
        text = plain
    return None


def jsessionid_of(cookie: str) -> str:
    m = re.search(r"JSESSIONID=([^;]+)", cookie)
    if not m:
        raise ValueError("Cookie 里没有 JSESSIONID")
    return m.group(1)


def build_headers(url: str, plain: str, cookie: str, xk: str) -> dict:
    """按抓包还原签名头。x-time/x-nonce 每次现取，x-sign 用当前 JSESSIONID 值。"""
    jsid = jsessionid_of(cookie)
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(10_000_000, 99_999_999))
    path = re.sub(r"^https?://[^/]+", "", url)
    token = base64.b64encode(
        _cbc(TOK_KEY, TOK_IV, f"{xk}_{path}_{ts}_{nonce}".encode(), True)
    ).decode()
    return {
        "x-qen": "14",
        "Accept": "*/*",
        "xs": hashlib.md5(f"{url}_{plain}_{SALT}".encode()).hexdigest(),
        "x-sign": hashlib.md5(f"{token}_{ts}_{nonce}_{jsid}".encode()).hexdigest(),
        "x-nonce": nonce,
        "x-token": token,
        "Accept-Encoding": "deflate",
        "Content-Type": "application/Json",
        "x-time": ts,
        "User-Agent": UA,
        "Cookie": cookie,
    }


# ---------------------------------------------------------------- profile

def profile_file(phone: str) -> Path:
    """按手机尾号隔离（不同账号的 xk / cellNum 不同）。"""
    return ROOT / f".cmcc_session_{phone[-4:]}.json"


def load_profile_from_har(path: str) -> dict:
    """从 HAR 取 autoLogin 请求，还原 cookie + 明文模板 + xk。

    兼容 LN 下多种 autoLogin（uamonekeylogin / uamrandcodelogin 等）：它们签名与
    加密一致，仅路径不同。抓包时 App 可能还没拿到会话，Cookie 头为空也接受
    （mint_cookie 本就用随机 UUID 占位）。
    """
    har = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = []
    for entry in har["log"]["entries"]:
        req = entry["request"]
        hdr = {h["name"].lower(): h["value"] for h in req["headers"]}
        body = (req.get("postData") or {}).get("text") or ""
        if not hdr.get("x-token") or not body:
            continue
        try:
            plain = _cbc(REQ_KEY, REQ_IV, base64.b64decode(body), False).decode()
            template = json.loads(plain)
        except Exception:
            continue
        if not template.get("xk"):
            continue
        is_autologin = req["url"].rstrip("/").endswith("/autoLogin")
        candidates.append((0 if is_autologin else 1, {
            "url": req["url"] if is_autologin
                   else "https://client.app.coc.10086.cn" + REFRESH_PATH,
            "cookie": hdr.get("cookie") or "",
            "template": template,
            "xk": template["xk"],
            "phone": str(template.get("tel") or ""),
            "captured_at": entry.get("startedDateTime"),
        }))
    if not candidates:
        if not any((e["request"].get("postData") or {}).get("text")
                   for e in har["log"]["entries"]):
            raise SystemExit(
                "该 HAR 只有 CONNECT 隧道（HTTPS 未解密），取不到请求体："
                "抓包工具需对该域名开启 SSL Proxying，并在手机上安装/信任其根证书后重抓"
            )
        raise SystemExit("HAR 里没找到可解的 biz-orange 加密请求（需带 x-token 与 cookie）")
    return min(candidates, key=lambda c: c[0])[1]


def load_profile(phone_hint: str = "") -> dict:
    files = sorted(ROOT.glob(".cmcc_session_*.json"))
    if phone_hint:
        files = [p for p in files if p.stem.endswith(phone_hint[-4:])]
    if not files:
        raise SystemExit("没有 profile，先用 --har <抓包.har> 提取一次")
    if len(files) > 1 and not phone_hint:
        raise SystemExit(
            f"有多个 profile（{', '.join(p.stem for p in files)}），用 --phone 指定手机尾号"
        )
    return json.loads(files[0].read_text(encoding="utf-8"))


def save_profile(profile: dict) -> Path:
    phone = str(profile.get("phone") or profile["template"].get("tel") or "")
    if not re.fullmatch(r"1\d{10}", phone):
        raise SystemExit(f"手机号不合法: {phone!r}")
    profile["phone"] = phone
    path = profile_file(phone)
    path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    return path


def update_config_token(config_path: str, phone: str, cookie: str) -> bool:
    """把续期后的新会话写回 config.json 的 app_token，其余脚本免改。

    校验 phone 一致，防止多账号串号。返回是否写入成功。
    """
    if not config_path:
        return False
    p = Path(config_path)
    if not p.exists():
        return False
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if str(cfg.get("phone") or "") != phone:
        log.warning("config %s 手机号与续期账号不一致，跳过回写", p.name)
        return False
    cfg["app_token"] = cookie
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------- 续期

def mint_state_file(phone: str) -> Path:
    tail = re.sub(r"\D", "", phone or "")[-4:] or "default"
    return ROOT / f".cmcc_mint_{tail}.json"


def _load_mint(phone: str) -> dict:
    try:
        return json.loads(mint_state_file(phone).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_mint(phone: str, **fields) -> dict:
    st = _load_mint(phone)
    st.update(fields)
    path = mint_state_file(phone)
    tmp = None
    try:
        # 临时文件 + os.replace：并发进程（Widget 每 15 分钟 / 定时脚本）读不到空或
        # 半截 JSON，否则时间戳丢失会让最短现签间隔失效，两个进程同时现签即风控诱因。
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        log.warning("现签状态写入失败: %s", e)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return st


def reusable_cookie(phone: str, now: float | None = None) -> str | None:
    """未过复用窗口且未被风控静默的现签会话，供各脚本跨进程共用。"""
    now = time.time() if now is None else now
    st = _load_mint(phone)
    if float(st.get("blocked_until") or 0) > now:
        return None
    cookie = str(st.get("cookie") or "")
    if cookie and 0 <= now - float(st.get("ts") or 0) < MINT_TTL:
        return cookie
    return None


def mint_blocked_reason(phone: str, now: float | None = None) -> str:
    """返回不能现签的原因；空串表示可以现签。"""
    now = time.time() if now is None else now
    st = _load_mint(phone)
    until = float(st.get("blocked_until") or 0)
    if until > now:
        code = st.get("blocked_code") or ""
        return f"风控静默中（{code}，{(until - now) / 3600:.1f} 小时后解禁）"
    if 0 <= now - float(st.get("ts") or 0) < MINT_MIN_INTERVAL:
        return f"距上次现签不足 {MINT_MIN_INTERVAL // 60} 分钟"
    return ""


def invalidate_minted(phone: str) -> None:
    """会话被服务端拒绝时清掉缓存；冷却仍在，避免失败后立刻补签。

    连续失效达阈值则退避：业务接口持续报会话失效时，每轮运行都重签等于反复登录，
    只会把封控拖长（最坏 1 次/10 分钟 = 144 次/天）。
    """
    _MINTED.pop(phone, None)
    st = _load_mint(phone)
    streak = int(st.get("dead_streak") or 0) + 1
    if streak >= DEAD_BACKOFF_AFTER:
        _save_mint(phone, cookie="", ts=time.time(), dead_streak=streak,
                   blocked_until=time.time() + DEAD_BACKOFF, blocked_code="session-dead")
        log.error("会话连续 %d 次被判失效，暂停现签 %.0f 小时（避免把封控拖长）",
                  streak, DEAD_BACKOFF / 3600)
        return
    _save_mint(phone, cookie="", dead_streak=streak)


def _record_mint_ok(phone: str, cookie: str) -> None:
    """现签成功：只刷新 cookie/ts 与风控静默，**不动** dead_streak。

    业务侧 410000/210000 必然紧跟在一次现签之后（先用现签会话，再被判失效），
    这里若把计数清零，连续失效计数永远封顶 1，退避阈值就形同虚设。
    计数只由业务成功（note_session_ok）清零。
    """
    _save_mint(phone, cookie=cookie, ts=time.time(), blocked_until=0, blocked_code="")


def note_session_ok(phone: str) -> None:
    """业务接口成功一次，清零连续失效计数（否则计数会一直累积到误退避）。"""
    if _load_mint(phone).get("dead_streak"):
        _save_mint(phone, dead_streak=0)


def risk_blocked_until(phone: str) -> float:
    """风控静默截止时间戳（0 表示未被封停）；冷却（MINT_MIN_INTERVAL）不算封停。"""
    return float(_load_mint(phone).get("blocked_until") or 0)


def _record_risk(phone: str, code: str) -> None:
    silence = RISK_SILENCE.get(code)
    if not silence:
        return
    _MINTED.pop(phone, None)
    _save_mint(phone, cookie="", ts=time.time(),
               blocked_until=time.time() + silence, blocked_code=code)
    log.error("命中 %s：%.1f 小时内不再自动登录（继续重试只会延长封控）",
              code, silence / 3600)


def last_error_hint() -> str:
    code = str(LAST_ERROR.get("retCode") or "")
    desc = LAST_ERROR.get("retDesc") or ""
    if code in RISK_SILENCE:
        return f"autoLogin 失败（{code} {desc}），已暂停重试"
    return f"autoLogin 失败：{desc}" if desc else ""


def mint_cookie(profile: dict, timeout: int = 25) -> str | None:
    """现签一张新会话 cookie（复用窗口内跨进程共用，避免把账号打成「登录过于频繁」）。

    autoLogin 不校验来者的 JSESSIONID，故这里用随机 UUID 占位即可（只需 Cookie
    头存在，供 x-sign 拼串）。签名/加密其余部分必须用抓包还原的模板。
    成功返回 cookie 字符串；失败返回 None（调用方决定是否回退）。
    """
    key = str(profile.get("phone") or "")
    now = time.time()
    hit = _MINTED.get(key)
    if hit and now - hit[0] < MINT_TTL:
        return hit[1]

    cached = reusable_cookie(key, now)
    if cached:
        # 复用起点沿用磁盘上 cookie 的原始 ts，不能重置为 now（否则 8h 窗口重新起算，
        # 进程内会一直用到服务端已回收的过期会话）。
        _MINTED[key] = (float(_load_mint(key).get("ts") or now), cached)
        return cached

    reason = mint_blocked_reason(key, now)
    if reason:
        log.info("跳过现签：%s", reason)
        LAST_ERROR.clear()
        LAST_ERROR.update({"retCode": "", "retDesc": reason})
        return None

    url = profile["url"]
    plain = json.dumps(profile["template"], separators=(",", ":"), ensure_ascii=False)
    placeholder = f"JSESSIONID={uuid.uuid4()}; UID=placeholder"
    headers = build_headers(url, plain, placeholder, profile["xk"])

    s = requests.Session()
    s.trust_env = False  # 不继承系统代理，避免抓包工具 MITM 证书干扰
    s.mount("https://", LegacyAdapter())
    # 先记时间戳：失败也算一次尝试，冷却期内不会再打登录接口
    _save_mint(key, ts=time.time())
    try:
        r = s.post(url, data=encrypt_body(plain).encode(), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        LAST_ERROR.clear()
        LAST_ERROR.update({"retCode": "", "retDesc": f"网络异常: {e}"})
        log.warning("现签会话网络异常: %s", e)
        return None

    resp = decrypt_response(r.content)
    if resp is None or str(resp.get("retCode")) != "000000":
        code = str((resp or {}).get("retCode") or "")
        desc = (resp or {}).get("retDesc") or ""
        LAST_ERROR.clear()
        LAST_ERROR.update({"retCode": code, "retDesc": desc})
        log.warning("现签会话失败: %s %s", code, desc or r.content[:60])
        _record_risk(key, code)
        return None
    cookie = r.headers.get("Set-Cookie")
    LAST_ERROR.clear()  # 成功即清掉上一轮风控码，否则后续失败会误报「已静默」
    if cookie:
        _MINTED[key] = (time.time(), cookie)
        _record_mint_ok(key, cookie)
    return cookie


def refresh(profile: dict, timeout: int = 25, config_path: str = "",
            force: bool = False) -> dict:
    """现签一张新会话并把 cookie / 有效期回写 profile（可选回写 config）。

    force=True 忽略现签冷却与风控静默（仅手动 --refresh --force 用）。
    返回 {ok, retCode, validityTerm, cookie} 或 {ok:False, reason, retCode, dead}。
    """
    phone = str(profile.get("phone") or "")
    reason = "" if force else mint_blocked_reason(phone)
    if reason:
        return {"ok": False, "reason": f"跳过现签：{reason}（确需换会话加 --refresh --force）"}

    url = profile["url"]
    # 明文必须与 xs 用同一份字符串，故统一从这里重建
    plain = json.dumps(profile["template"], separators=(",", ":"), ensure_ascii=False)
    # 抓包时若 App 尚未持有会话，profile 里 cookie 为空；autoLogin 不校验来者会话，
    # 用随机 UUID 占位即可（x-sign 需要 JSESSIONID 拼串）。
    cookie = profile.get("cookie") or ""
    if not re.search(r"JSESSIONID=", cookie):
        cookie = f"JSESSIONID={uuid.uuid4()}; UID=placeholder"
    headers = build_headers(url, plain, cookie, profile["xk"])

    s = requests.Session()
    s.trust_env = False  # 不继承系统代理，避免抓包工具 MITM 证书干扰
    s.mount("https://", LegacyAdapter())
    _save_mint(phone, ts=time.time())
    try:
        r = s.post(url, data=encrypt_body(plain).encode(), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return {"ok": False, "reason": f"网络异常: {e}"}

    if r.status_code == 403:
        return {"ok": False, "reason": "网关 403：xs/xk/x-qen/cv 校验失败，或出口 IP 被拒"}

    resp = decrypt_response(r.content)
    if resp is None:
        return {"ok": False, "reason": f"响应无法解密: {r.content[:80]!r}"}

    code = str(resp.get("retCode"))
    if code != "000000":
        _record_risk(phone, code)
        return {"ok": False, "retCode": code, "retDesc": resp.get("retDesc", ""),
                "dead": code in DEAD_CODES}

    body = resp.get("rspBody") or {}
    validity = body.get("validityTerm")
    new_cookie = r.headers.get("Set-Cookie")
    if new_cookie:
        profile["cookie"] = new_cookie
        profile["validity_term"] = validity
        profile["saved_at"] = time.time()
        save_profile(profile)
        _MINTED[phone] = (time.time(), new_cookie)
        _record_mint_ok(phone, new_cookie)
        LAST_ERROR.clear()
        # 顺带回写 config.json 的 app_token：新会话可直接用于 appTokenLogin
        if config_path and update_config_token(config_path, profile["phone"], new_cookie):
            log.info("已回写 %s 的 app_token", config_path)

    return {"ok": True, "retCode": code, "validityTerm": validity, "cookie": new_cookie,
            "province": body.get("provinceName"), "city": body.get("cityName")}


def describe(profile: dict) -> str:
    vt = profile.get("validity_term")
    if not vt:
        return "有效期未知（尚未续期过）"
    vt = int(vt)  # 服务端可能以字符串返回
    exp = datetime.fromtimestamp(vt / 1000)
    left = (vt / 1000 - time.time()) / 86400
    return f"会话有效期至 {exp:%Y-%m-%d %H:%M}（剩 {left:.1f} 天）"


# ---------------------------------------------------------------- 自检

def self_test() -> int:
    """离线校验加密链路（不联网）。

    用 xs 向量断言，确保 SALT、URL 拼法、字段布局都没写错。
    向量取自真实抓包，但手机号/设备指纹已替换为等长假值（算法与字段布局不变）。
    """
    url = "https://client.app.coc.10086.cn" + REFRESH_PATH
    real = ('{"tel":"15100000000","sv":"26.7.1","nt":"3","packageName":"cn.10086.app",'
            '"prov":"270","t":"0123456789abcdef0123456789abcdef","sn":"iPhone15,2",'
            '"imei":"0123456789ABCDEF0123456789ABCDEF","city":"027",'
            '"ctid":"0123456789ABCDEF0123456789ABCDEFA","appKey":"00000160","sp":"1179x2556",'
            '"en":"0","componentVersion":"1.0.0","reqBody":{"cellNum":"x"},'
            '"cid":"","xk":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",'
            '"xc":"B2000","componentID":"BI2021SI000024",'
            '"ak":"0123456789ABCDEF0123456789ABCDEF01234567","cv":"12.5.5","sb":"apple",'
            '"baseFrameVersion":"1.0.0","st":2}')

    # 1) AES 往返
    body = encrypt_body(real)
    assert _cbc(REQ_KEY, REQ_IV, base64.b64decode(body), False).decode() == real, "AES 往返失败"

    # 2) x-token 明文布局必须是 xk_path_time_nonce
    cookie = "JSESSIONID=afd2ca32-2e31-4ac6-a9e6-871519a56ec6; UID=x"
    h = build_headers(url, real, cookie, "01234567")
    tok = _cbc(TOK_KEY, TOK_IV, base64.b64decode(h["x-token"]), False).decode()
    assert tok == f"01234567_{REFRESH_PATH}_{h['x-time']}_{h['x-nonce']}", tok
    assert jsessionid_of(cookie) == "afd2ca32-2e31-4ac6-a9e6-871519a56ec6"

    # 3) x-sign 公式
    assert h["x-sign"] == hashlib.md5(
        f"{h['x-token']}_{h['x-time']}_{h['x-nonce']}_afd2ca32-2e31-4ac6-a9e6-871519a56ec6".encode()
    ).hexdigest()

    # 4) xs 向量：与 refreshSession 请求的字段布局一致（已脱敏）
    rs_url = "https://client.app.coc.10086.cn/biz-orange/DN/refreshSession"
    rs_plain = ('{"tel":"15100000000","sv":"26.7.1","nt":"3","sn":"iPhone15,2","prov":"270",'
                '"t":"0123456789abcdef0123456789abcdef","imei":"0123456789ABCDEF0123456789ABCDEF",'
                '"city":"027","ctid":"0123456789ABCDEF0123456789ABCDEFA","en":"0","sp":"1179x2556",'
                '"reqBody":{"cellNum":"15100000000"},"cid":"",'
                '"xk":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",'
                '"xc":"B2000","ak":"0123456789ABCDEF0123456789ABCDEF01234567","cv":"12.5.5",'
                '"sb":"apple","st":2}')
    got = hashlib.md5(f"{rs_url}_{rs_plain}_{SALT}".encode()).hexdigest()
    assert got == "b52a75aa79e6e506222c6199cc8be97c", got

    _self_test_throttle()
    print("self-test OK：AES 往返、x-token 布局、x-sign 公式、xs 向量、现签冷却全部通过")
    return 0


def _self_test_throttle() -> None:
    """离线校验会话复用窗口、最短现签间隔与风控静默（不联网）。"""
    global ROOT
    old_root, now = ROOT, 1_000_000.0
    ROOT = Path(tempfile.mkdtemp())
    try:
        phone = "13800000000"
        assert reusable_cookie(phone, now) is None
        assert mint_blocked_reason(phone, now) == ""

        _save_mint(phone, cookie="JSESSIONID=a", ts=now - 60)
        assert reusable_cookie(phone, now) == "JSESSIONID=a"
        assert reusable_cookie(phone, now + MINT_TTL) is None
        assert mint_blocked_reason(phone, now + MINT_TTL) == ""

        _save_mint(phone, cookie="", ts=now)
        assert "不足" in mint_blocked_reason(phone, now + 60)
        assert mint_blocked_reason(phone, now + MINT_MIN_INTERVAL + 1) == ""

        _save_mint(phone, cookie="", ts=now, blocked_until=now + 3600, blocked_code="214103")
        assert reusable_cookie(phone, now + 60) is None
        assert "214103" in mint_blocked_reason(phone, now + 60)
        assert mint_blocked_reason(phone, now + 3600 + MINT_MIN_INTERVAL + 1) == ""

        now_real = time.time()
        _save_mint(phone, cookie="", ts=now_real, blocked_until=0, blocked_code="")
        _record_risk(phone, "214100")
        st = _load_mint(phone)
        assert st["blocked_code"] == "214100", st
        assert abs(st["blocked_until"] - (now_real + RISK_SILENCE["214100"])) < 5, st
        assert mint_blocked_reason(phone, now_real) != ""

        # 原子写：写完只剩状态文件本身，没有遗留 .tmp（并发进程不会读到半截）
        assert [p.name for p in ROOT.iterdir() if p.name.endswith(".tmp")] == [], list(ROOT.iterdir())

        # 连续失效退避（真实路径：现签成功 → 业务判失效 → 现签 → …）：
        # 现签成功不清计数，前两次只清缓存，第 3 次才封停；业务成功一次即清零。
        _save_mint(phone, cookie="JSESSIONID=b", ts=now_real, blocked_until=0,
                   blocked_code="", dead_streak=0)
        invalidate_minted(phone)
        invalidate_minted(phone)
        assert not _load_mint(phone).get("blocked_until"), _load_mint(phone)
        _record_mint_ok(phone, "JSESSIONID=b2")  # 每次失效前必有一次现签，不能清零计数
        assert int(_load_mint(phone)["dead_streak"]) == 2, _load_mint(phone)
        invalidate_minted(phone)
        assert _load_mint(phone)["blocked_code"] == "session-dead", _load_mint(phone)
        assert mint_blocked_reason(phone, now_real) != ""
        note_session_ok(phone)
        assert int(_load_mint(phone).get("dead_streak") or 0) == 0

        # 磁盘缓存命中时复用起点必须沿用原始 ts，否则窗口会从 now 重新起算
        _MINTED.clear()
        _save_mint(phone, cookie="JSESSIONID=c", ts=now_real - 300, blocked_until=0,
                   blocked_code="", dead_streak=0)
        assert mint_cookie({"phone": phone}) == "JSESSIONID=c"
        assert abs(_MINTED[phone][0] - (now_real - 300)) < 5, _MINTED[phone]
        _MINTED.clear()
    finally:
        ROOT = old_root


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="移动 App 会话签发（autoLogin）")
    ap.add_argument("--har", help="抓包 HAR：提取 cookie/模板/xk 并存 profile（一次即可）")
    ap.add_argument("--refresh", action="store_true", help="现签一张新会话")
    ap.add_argument("--show", action="store_true", help="查看当前会话剩余有效期")
    ap.add_argument("--phone", default="", help="多账号时指定手机尾号")
    ap.add_argument("--config", default="", help="现签成功后回写该 config.json 的 app_token")
    ap.add_argument("--print-template", action="store_true",
                    help="打印 autoLogin 明文模板（JSON），供复制进 config.json 的 session_template")
    ap.add_argument("--force", action="store_true",
                    help="配 --refresh 用：忽略现签冷却与风控静默，强制现签一次")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--self-test", action="store_true", help="离线自检加密链路")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    if args.self_test:
        return self_test()

    if args.har:
        profile = load_profile_from_har(args.har)
        path = save_profile(profile)
        log.info("HAR 解析成功：手机尾号 %s，模板 %d 字段，profile 已存 %s",
                 profile["phone"][-4:], len(profile["template"]), path.name)
    elif args.refresh or args.show or args.print_template:
        profile = load_profile(args.phone)
    else:
        ap.error("需要 --har <抓包.har> / --refresh / --show / --print-template")

    if args.show and not args.refresh:
        print(describe(profile))
        return 0

    if args.print_template:
        print(json.dumps(profile["template"], ensure_ascii=False, indent=2))
        return 0

    res = refresh(profile, args.timeout, args.config, args.force)
    if res["ok"]:
        log.info("现签成功：%s %s，%s", res.get("province") or "",
                 res.get("city") or "", describe(profile))
        return 0
    if res.get("dead"):
        reason, hint = DEAD_CODES.get(res["retCode"], ("未知原因", "请重新冷启动抓包后用 --har 导入"))
        log.error("会话不可用（%s %s）：%s，%s",
                  res["retCode"], res.get("retDesc", ""), reason, hint)
        return 1
    log.error("现签失败：%s %s", res.get("retCode") or "-",
              res.get("retDesc") or res.get("reason"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
