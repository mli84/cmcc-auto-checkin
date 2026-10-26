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
import random
import re
import ssl
import sys
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

# 服务端明确表示「本次登录不可继续」的返回码
DEAD_CODES = {
    "410000": "会话空闲超时",
    "233031": "登录请求过于频繁（同号短时间多次续期，稍后重试）",
    "210000": "会话已失效",
}

log = logging.getLogger("cmcc_session")


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
    """从 HAR 取 autoLogin 请求，还原 cookie + 明文模板 + xk。"""
    har = json.loads(Path(path).read_text(encoding="utf-8"))
    candidates = []
    for entry in har["log"]["entries"]:
        req = entry["request"]
        hdr = {h["name"].lower(): h["value"] for h in req["headers"]}
        body = (req.get("postData") or {}).get("text") or ""
        if not hdr.get("x-token") or not hdr.get("cookie") or not body:
            continue
        try:
            plain = _cbc(REQ_KEY, REQ_IV, base64.b64decode(body), False).decode()
            template = json.loads(plain)
        except Exception:
            continue
        if not template.get("xk"):
            continue
        # 优先 autoLogin（续期接口），退而取任意 biz-orange 请求
        is_autologin = req["url"].endswith(REFRESH_PATH)
        candidates.append((0 if is_autologin else 1, {
            "url": req["url"] if is_autologin
                   else "https://client.app.coc.10086.cn" + REFRESH_PATH,
            "cookie": hdr["cookie"],
            "template": template,
            "xk": template["xk"],
            "phone": str(template.get("tel") or ""),
            "captured_at": entry.get("startedDateTime"),
        }))
    if not candidates:
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

def mint_cookie(profile: dict, timeout: int = 25) -> str | None:
    """现签一张新会话 cookie。

    autoLogin 不校验来者的 JSESSIONID，故这里用随机 UUID 占位即可（只需 Cookie
    头存在，供 x-sign 拼串）。签名/加密其余部分必须用抓包还原的模板。
    成功返回 cookie 字符串；失败返回 None（调用方决定是否回退）。
    """
    url = profile["url"]
    plain = json.dumps(profile["template"], separators=(",", ":"), ensure_ascii=False)
    placeholder = f"JSESSIONID={uuid.uuid4()}; UID=placeholder"
    headers = build_headers(url, plain, placeholder, profile["xk"])

    s = requests.Session()
    s.trust_env = False  # 不继承系统代理，避免抓包工具 MITM 证书干扰
    s.mount("https://", LegacyAdapter())
    try:
        r = s.post(url, data=encrypt_body(plain).encode(), headers=headers, timeout=timeout)
    except requests.RequestException as e:
        log.warning("现签会话网络异常: %s", e)
        return None

    resp = decrypt_response(r.content)
    if resp is None or str(resp.get("retCode")) != "000000":
        log.warning("现签会话失败: %s %s", (resp or {}).get("retCode"),
                    (resp or {}).get("retDesc") or r.content[:60])
        return None
    return r.headers.get("Set-Cookie")


def refresh(profile: dict, timeout: int = 25, config_path: str = "") -> dict:
    """现签一张新会话并把 cookie / 有效期回写 profile（可选回写 config）。

    返回 {ok, retCode, validityTerm, cookie} 或 {ok:False, reason, retCode, dead}。
    """
    url = profile["url"]
    # 明文必须与 xs 用同一份字符串，故统一从这里重建
    plain = json.dumps(profile["template"], separators=(",", ":"), ensure_ascii=False)
    headers = build_headers(url, plain, profile["cookie"], profile["xk"])

    s = requests.Session()
    s.trust_env = False  # 不继承系统代理，避免抓包工具 MITM 证书干扰
    s.mount("https://", LegacyAdapter())
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

    用真实抓包的 xs 向量断言，确保 SALT、URL 拼法、字段布局都没写错。
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

    # 4) 已知抓包向量：xs 与 refreshSession 那条真实请求逐字节一致
    rs_url = "https://client.app.coc.10086.cn/biz-orange/DN/refreshSession"
    rs_plain = ('{"tel":"15100000000","sv":"26.7.1","nt":"3","sn":"iPhone15,2","prov":"270",'
                '"t":"0123456789abcdef0123456789abcdef","imei":"0123456789ABCDEF0123456789ABCDEF",'
                '"city":"027","ctid":"0123456789ABCDEF0123456789ABCDEFA","en":"0","sp":"1179x2556",'
                '"reqBody":{"cellNum":"15100000000"},"cid":"",'
                '"xk":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",'
                '"xc":"B2000","ak":"0123456789ABCDEF0123456789ABCDEF01234567","cv":"12.5.5",'
                '"sb":"apple","st":2}')
    got = hashlib.md5(f"{rs_url}_{rs_plain}_{SALT}".encode()).hexdigest()
    assert got == "dff23a45ab1ad0ae17c6ae4098b41a37", got

    print("self-test OK：AES 往返、x-token 布局、x-sign 公式、真实抓包 xs 向量全部通过")
    return 0


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

    res = refresh(profile, args.timeout, args.config)
    if res["ok"]:
        log.info("现签成功：%s %s，%s", res.get("province") or "",
                 res.get("city") or "", describe(profile))
        return 0
    if res.get("dead"):
        log.error("会话不可用（%s %s）：%s，请重新冷启动抓包后用 --har 导入",
                  res["retCode"], res.get("retDesc", ""),
                  DEAD_CODES.get(res["retCode"], "未知原因"))
        return 1
    log.error("现签失败：%s %s", res.get("retCode") or "-",
              res.get("retDesc") or res.get("reason"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
