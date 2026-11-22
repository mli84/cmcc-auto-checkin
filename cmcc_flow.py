#!/usr/bin/env python3
"""流量余量 → iPhone 桌面 Widget（Scriptable）。

抓 clientaccess 的「套餐余量」接口，归一化成一份 JSON 写进 iCloud Drive 的
Scriptable 目录；Widget 只读本地文件，凭证与签名全部留在 Mac 上。

    python3 cmcc_flow.py                # 抓一次并落盘（launchd 每 15 分钟跑这个）
    python3 cmcc_flow.py --dry-run      # 只打印，不落盘、不推送
    python3 cmcc_flow.py --self-test    # 离线自检：用 fixtures/flow_sample.json 验解析
    python3 cmcc_flow.py --install-launchd   # 安装并加载 launchd（每 15 分钟）

接口事实（2026-11-20 抓包 + 实测，与 ha-china-mobile 的 BIZ_KEYS 一致）：
  POST https://clientaccess.10086.cn/biz-orange/BH/newPlanRemainQry/getNewPlanRemainQry
  请求 AES-128-CBC bAIgvwAuA4tbDr9d / 响应 AES-128-CBC GS7VelkJl5IT1uwQ / iv 9791027341711819
  明文 reqBody.cellNum 是**明文手机号**（不是登录接口那个 RSA cellNum）
  xs    = md5(url_明文_SALT)
  x-token = base64(AES(xk_path_x-time_x-nonce))，xk 取自 .cmcc_session_<尾号>.json
  会话由 cmcc_session.mint_cookie() 现签，无需人工抓包、无需保活
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cmcc_session as cs  # noqa: E402

HOST = "clientaccess.10086.cn"
PATH = "/biz-orange/BH/newPlanRemainQry/getNewPlanRemainQry"
URL = f"https://{HOST}{PATH}"
REQ_KEY, RESP_KEY = b"bAIgvwAuA4tbDr9d", b"GS7VelkJl5IT1uwQ"
IV = b"9791027341711819"
SALT = "Leadeon/SecurityOrganization"
UA = "ChinaMobile/12.5.6 (iPhone; iOS 26.7.1; Scale/3.00)"
# 业务明文只带这些键（照抄抓包顺序；模板里多出来的字段服务端不需要，也不该发）
BIZ_FIELDS = ("tel", "sv", "nt", "sn", "prov", "t", "imei", "city", "ctid",
              "en", "sp", "reqBody", "cid", "xk", "xc", "ak", "cv", "sb", "st")
UNIT = {"03": 1.0, "04": 1024.0}          # 03=MB, 04=GB → 统一换算成 MB
NIGHT_START, NIGHT_END = 0, 7              # 该时段不抓（Widget 显示"过期"即可）
SESSION_DEAD = ("410000", "210000")        # 只有这些码值得清缓存重签，其他失败重签也没用
ICLOUD = Path.home() / "Library/Mobile Documents/iCloud~dk~simonbs~Scriptable/Documents"


# ------------------------------------------------------------------ 接口
def _decrypt(raw: bytes, key: bytes) -> dict:
    """响应体解密。线上是单层 base64，Proxyman 导出的 HAR 是两层，逐层试。"""
    text = raw.decode("utf-8", "replace").strip()
    for _ in (1, 2):
        try:
            plain = cs._cbc(key, IV, base64.b64decode(text), False).decode()
        except Exception:
            break
        if plain.startswith("{"):
            return json.loads(plain)
        text = plain
    raise RuntimeError(f"响应解不开: {raw[:60]!r}")


def _cookie_of(set_cookie: str) -> str:
    """Set-Cookie → 只留服务端认的那几个。"""
    keep = []
    for name in ("JSESSIONID", "UID", "ticketID"):
        m = re.search(name + r"=([^;,\s]+)", set_cookie)
        if m:
            keep.append(f"{name}={m.group(1)}")
    return "; ".join(keep)


def fetch(profile: dict, timeout: int = 25) -> dict:
    cookie = cs.mint_cookie(profile)
    if not cookie:
        hint = cs.last_error_hint()
        raise RuntimeError("现签会话失败" + (f"（{hint}）" if hint else "（见上方 cmcc_session 日志）"))
    cookie = _cookie_of(cookie)
    jsid = cs.jsessionid_of(cookie)

    tmpl = profile["template"]
    plain = json.dumps(
        {k: ({"cellNum": tmpl["tel"]} if k == "reqBody" else tmpl.get(k, "")) for k in BIZ_FIELDS},
        separators=(",", ":"), ensure_ascii=False,
    )
    ts = str(int(time.time() * 1000))
    nonce = str(random.randint(10_000_000, 99_999_999))
    token = base64.b64encode(cs._cbc(REQ_KEY, IV, f"{tmpl['xk']}_{PATH}_{ts}_{nonce}".encode(), True)).decode()
    headers = {
        "x-qen": "2",
        "Accept": "*/*",
        "xs": hashlib.md5(f"{URL}_{plain}_{SALT}".encode()).hexdigest(),
        "x-sign": hashlib.md5(f"{token}_{ts}_{nonce}_{jsid}".encode()).hexdigest(),
        "x-nonce": nonce,
        "x-token": token,
        "Accept-Encoding": "deflate",
        "Content-Type": "application/Json",
        "x-time": ts,
        "User-Agent": UA,
        "Cookie": cookie,
    }
    s = requests.Session()
    s.trust_env = False
    s.mount("https://", cs.LegacyAdapter())
    r = s.post(URL, data=base64.b64encode(cs._cbc(REQ_KEY, IV, plain.encode(), True)).decode(),
               headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    out = _decrypt(r.content, RESP_KEY)
    code = str(out.get("retCode"))
    if code != "000000":
        if code in SESSION_DEAD:
            # 会话被服务端回收/顶号：清掉复用缓存，下次跑重新现签（仍受最短间隔约束）
            cs.invalidate_minted(str(profile.get("phone") or ""))
        raise RuntimeError(f"{code} {out.get('retDesc') or ''}".strip())
    return out


# ------------------------------------------------------------------ 归一化
def _mb(value, unit) -> float:
    try:
        return float(value) * UNIT.get(str(unit), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _merge(plans: dict, plan: dict) -> None:
    """同 planID 合并（"上月结转"和本月是两条）。有效期取最早，名字取不带结转标记的。"""
    cur = plans.get(plan["id"])
    if not cur:
        plans[plan["id"]] = plan
        return
    for k in ("total_mb", "used_mb", "remain_mb"):
        cur[k] += plan[k]
    if plan["expire"] and (not cur["expire"] or plan["expire"] < cur["expire"]):
        cur["expire"] = plan["expire"]
    if "结转" in cur["name"] and "结转" not in plan["name"]:
        cur["name"] = plan["name"]


def _entries(out: dict) -> list[dict]:
    """接口 → 原始套餐条目（未合并、未过滤）。定向流量（flowtype=1）不要。"""
    res = (out.get("rspBody") or {}).get("newPlanRemainQryRes") or {}
    rows = []
    for group in ((res.get("planRemianFlowListRes") or {}).get("planRemianFlowRes") or []):
        if str(group.get("flowtype")) != "0":
            continue
        for p in group.get("planRemianFlowInfoRes") or []:
            flows = p.get("flowInfoRes") or []
            if not flows:
                continue
            rows.append({
                "id": p.get("planID") or p.get("planName") or "",
                "name": (p.get("planName") or "").strip(),
                "expire": (p.get("expireDate") or "").strip(),
                # expireFlag=1 是服务端标的"已失效"，日包过期后也要按时间判
                "expired": str(p.get("expireFlag")) == "1",
                "total_mb": sum(_mb(f.get("totalRes"), f.get("unit")) for f in flows),
                "used_mb": sum(_mb(f.get("usedRes"), f.get("unit")) for f in flows),
                "remain_mb": sum(_mb(f.get("remainRes"), f.get("unit")) for f in flows),
            })
    return rows


def normalize(out: dict, now: float | None = None) -> dict:
    now = now or time.time()
    res = (out.get("rspBody") or {}).get("newPlanRemainQryRes") or {}
    flist = res.get("planRemianFlowListRes") or {}
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    rows = _entries(out)
    # 先按失效与否分开，再在各自的池子里按 planID 合并。日包的 planID 会被不同批次复用，
    # 先合并会把"本月有效"和"上周已失效"混成一条，所以顺序不能反。
    gone = [r for r in rows if r["expired"] or (r["expire"] and r["expire"] < now_str)]
    dead_ids = {id(r) for r in gone}
    alive: dict[str, dict] = {}
    for row in rows:
        if id(row) not in dead_ids:
            _merge(alive, row)
    last_gone = max(gone, key=lambda p: p["expire"]) if gone else None
    live = alive.values()
    return {
        "updated_at": int(now),
        "ok": True,
        "error": None,
        "total": {
            "total_mb": round(sum(p["total_mb"] for p in live), 1),
            "used_mb": round(sum(p["used_mb"] for p in live), 1),
            "remain_mb": round(sum(p["remain_mb"] for p in live), 1),
            "server_remain_mb": _mb(flist.get("limiteRemainNum"), flist.get("unit")),
        },
        "packages": [
            {k: (round(p[k], 1) if k.endswith("_mb") else p[k]) for k in
             ("id", "name", "total_mb", "used_mb", "remain_mb", "expire")}
            for p in sorted(live, key=lambda p: (p["expire"] or "9999", -p["remain_mb"]))
        ],
        "expired_last": ({k: (round(last_gone[k], 1) if k.endswith("_mb") else last_gone[k]) for k in
                          ("id", "name", "total_mb", "used_mb", "remain_mb", "expire")} if last_gone else None),
    }


# ------------------------------------------------------------------ 状态 / 推送
def state_file(tail: str) -> Path:
    return Path(__file__).resolve().parent / f".cmcc_flow_state_{tail}.json"


def load_state(tail: str) -> dict:
    try:
        return json.loads(state_file(tail).read_text())
    except (OSError, ValueError):
        return {"fail": 0, "pushes": {}, "daily": []}


def save_state(tail: str, data: dict) -> None:
    state_file(tail).write_text(json.dumps(data, ensure_ascii=False))


def notify(bark_url: str, title: str, body: str) -> None:
    if not bark_url:
        return
    try:
        requests.get(f"{bark_url.rstrip('/')}/{quote(title)}/{quote(body)}",
                     timeout=10)
    except requests.RequestException:
        pass


def write_json(path: Path, data: dict) -> None:
    """先写临时文件再 rename：Widget 永远不会读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def snapshot_and_push(snap: dict, state: dict, tail: str, bark_url: str, now: float) -> None:
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    # 接口给的是"本月累计已用"，会跨月归零，所以每天的增量必须用 cum_mb 相减得出，
    # 不能拿 used_mb（日增量）当基准。
    daily = [d for d in state.get("daily", []) if d.get("d") != today]
    entry = {"d": today, "cum_mb": round(snap["total"]["used_mb"], 1)}
    prev = daily[-1] if daily else None
    if prev and "cum_mb" in prev:
        entry["used_mb"] = round(max(0.0, entry["cum_mb"] - prev["cum_mb"]), 1)
    daily.append(entry)
    state["daily"] = daily[-30:]
    used = [d["used_mb"] for d in daily if d["d"] != today and "used_mb" in d][-7:]
    snap["avg_daily_mb_7d"] = round(sum(used) / len(used), 1) if used else None

    pushes = state.setdefault("pushes", {})
    remain = snap["total"]["remain_mb"]
    total = snap["total"]["total_mb"]
    if total and (remain < total * 0.1 or remain < 1024) and pushes.get("low") != today:
        notify(bark_url, "流量快用完了", f"剩余 {remain:.0f} MB（{remain / total * 100:.0f}%），共 {total / 1024:.1f} GB")
        pushes["low"] = today

    before = {p["id"]: p["remain_mb"] for p in state.get("packages", [])}
    for pid, was in before.items():
        if was <= 0:
            continue
        now_remain = next((p["remain_mb"] for p in snap["packages"] if p["id"] == pid), None)
        if now_remain is None or now_remain <= 0:      # 用完了，或整包从列表消失
            name = next((p["name"] for p in state["packages"] if p["id"] == pid), pid)
            key = f"done:{pid}"
            if not pushes.get(key):
                notify(bark_url, "流量包用完了", name)
                pushes[key] = today
    state["packages"] = snap["packages"]


# ------------------------------------------------------------------ 主流程
def run(config_path: str, dry: bool, force: bool) -> int:
    cfg = json.loads(Path(config_path).read_text())
    phone, bark = str(cfg.get("phone") or ""), cfg.get("bark_url") or ""
    if not phone:
        print("config 里没有 phone", file=sys.stderr)
        return 2
    tail = phone[-4:]
    now = time.time()
    if not force and NIGHT_START <= time.localtime(now).tm_hour < NIGHT_END:
        print("夜间时段，跳过")
        return 0

    if not dry and not ICLOUD.exists():
        print(f"找不到 {ICLOUD}\n先在 iPhone 上安装 Scriptable（免费）并打开一次，目录才会出现。", file=sys.stderr)
        return 3

    state = load_state(tail)
    blocked = cs.risk_blocked_until(tail)
    if blocked > now:
        print(f"风控静默中，跳过（至 {time.strftime('%m-%d %H:%M', time.localtime(blocked))}）")
        return 0
    try:
        snap = normalize(fetch(cs.load_profile(tail)))
        snap["phone_tail"] = tail
    except Exception as e:                        # noqa: BLE001 - 任何失败都只降级，不炸 launchd
        state["fail"] = int(state.get("fail", 0)) + 1
        save_state(tail, state)
        prev = None
        try:
            prev = json.loads((ICLOUD / f"flow_{tail}.json").read_text())
            prev.update(ok=False, error=str(e))
        except (OSError, ValueError):
            prev = {"updated_at": 0, "ok": False, "error": str(e), "phone_tail": tail}
        print(f"抓取失败({state['fail']}): {e}", file=sys.stderr)
        if state["fail"] == 3:
            notify(bark, "流量刷新连续失败", str(e))
        if not dry:
            write_json(ICLOUD / f"flow_{tail}.json", prev)
            save_state(tail, state)
        return 1

    recovered = int(state.get("fail", 0)) >= 3
    state["fail"] = 0
    snapshot_and_push(snap, state, tail, "" if dry else bark, now)
    snap["error"] = None
    if recovered:
        notify(bark, "流量刷新已恢复", f"剩余 {snap['total']['remain_mb']:.0f} MB")
    if dry:
        print(json.dumps(snap, ensure_ascii=False, indent=1))
        return 0
    write_json(ICLOUD / f"flow_{tail}.json", snap)
    save_state(tail, state)
    print(f"OK 剩余 {snap['total']['remain_mb']:.0f} MB / {len(snap['packages'])} 个包 → {ICLOUD / f'flow_{tail}.json'}")
    return 0


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cmcc.flow.{tail}</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>{script}</string>
    <string>--config</string><string>{config}</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{cwd}/flow.log</string>
  <key>StandardErrorPath</key><string>{cwd}/flow.log</string>
</dict></plist>
"""


def install_launchd(config_path: str) -> int:
    here = Path(__file__).resolve().parent
    cfg = json.loads(Path(config_path).resolve().read_text())
    tail = str(cfg.get("phone"))[-4:]
    label = f"com.cmcc.flow.{tail}"
    target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    target.write_text(PLIST.format(tail=tail, python=sys.executable, script=here / "cmcc_flow.py",
                                   config=Path(config_path).resolve(), cwd=here))
    subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
    subprocess.run(["launchctl", "load", str(target)], check=True)
    print(f"已加载 {target}（每 900 秒；00:00-07:00 自动跳过）")
    return 0


def self_test() -> int:
    fixture = Path(__file__).resolve().parent / "fixtures/flow_sample.json"
    snap = normalize(json.loads(fixture.read_text()), now=time.mktime((2026, 11, 20, 22, 42, 0, 0, 0, -1)))
    assert snap["ok"] is True
    assert 3900 < snap["total"]["remain_mb"] < 4100, snap["total"]["remain_mb"]
    assert abs(snap["total"]["remain_mb"] - snap["total"]["server_remain_mb"]) < 10, "我方累加与接口聚合值不符"
    assert len(snap["packages"]) == 7, [p["name"] for p in snap["packages"]]      # 同 planID 已合并
    assert snap["packages"][0]["expire"] == "2026-11-21 08:06:42", "应按到期时间升序"
    merged = next(p for p in snap["packages"] if p["id"] == "G245489C")
    assert merged["total_mb"] == 22147 and "结转" not in merged["name"], merged
    assert all(p["expire"] for p in snap["packages"]), "每个包都必须有有效期"
    assert snap["expired_last"] and snap["expired_last"]["expire"].startswith("2026-11-20")
    assert all("云盘" not in p["name"] for p in snap["packages"]), "定向流量不该出现"

    day, state = 86400, {"fail": 0, "pushes": {}, "daily": []}
    for i, cum in enumerate((1000.0, 1500.0, 1800.0)):
        snapshot_and_push({"total": {"total_mb": 5000.0, "used_mb": cum, "remain_mb": 5000.0 - cum},
                           "packages": []}, state, "0000", "", now=1795000000 + i * day)
    assert [d.get("used_mb") for d in state["daily"]] == [None, 500.0, 300.0], state["daily"]
    assert snap and state["daily"][-1]["cum_mb"] == 1800.0
    print("self-test OK", json.dumps(snap["total"], ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="流量余量 → Scriptable Widget")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘不推送")
    ap.add_argument("--force", action="store_true", help="忽略夜间时段限制")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--install-launchd", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.install_launchd:
        return install_launchd(a.config)
    return run(a.config, a.dry_run, a.force)


if __name__ == "__main__":
    sys.exit(main())
