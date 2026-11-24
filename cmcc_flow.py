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
  会话由 cmcc_session.mint_cookie() 提供：落盘结果跨进程复用 8 小时（MINT_TTL），
  只有业务接口回 410000 或真机 App 重登顶号时才清缓存重签（同一轮内立刻重签一次）；无需人工抓包、无需保活
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
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


def _query(cookie: str, tmpl: dict, timeout: int) -> dict:
    """用已签好的会话打一次套餐余量接口。"""
    cookie = _cookie_of(cookie)
    jsid = cs.jsessionid_of(cookie)

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
    return _decrypt(r.content, RESP_KEY)


def fetch(profile: dict, timeout: int = 25) -> dict:
    phone = str(profile.get("phone") or "")
    err = ""
    # 会话被回收/顶号（410000/210000）时清缓存后同轮重签一次：下一轮要等 15 分钟，
    # 白等一个刷新周期会让 Widget 一直红着报「请重新登录」。登录次数与旧逻辑相同（只是提前）。
    for _ in range(2):
        cookie = cs.mint_cookie(profile)
        if not cookie:
            hint = cs.last_error_hint()
            err = err or "现签会话失败" + (f"（{hint}）" if hint else "（见上方 cmcc_session 日志）")
            break
        out = _query(cookie, profile["template"], timeout)
        code = str(out.get("retCode"))
        if code == "000000":
            cs.note_session_ok(phone)
            return out
        err = f"{code} {out.get('retDesc') or ''}".strip()
        if code not in SESSION_DEAD:
            break
        cs.invalidate_minted(phone)
    raise RuntimeError(err)


# ------------------------------------------------------------------ 归一化
def _mb(value, unit) -> float:
    try:
        return float(value) * UNIT.get(str(unit), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _entries(out: dict) -> list[dict]:
    """接口 → 原始套餐条目（未过滤）。定向流量（flowtype=1）不要。"""
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
                # 一次发放一行：日包每次发奖都复用同一个 planID（"统一营销流量日包-100MB"
                # 领 4 次就是 4 条），必须带上 validDate 才能和 state 里的一一对应。
                "id": "|".join(x for x in (p.get("planID"), p.get("validDate")) if x)
                      or p.get("planName") or "",
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
    gone = [r for r in rows if r["expired"] or (r["expire"] and r["expire"] < now_str)]
    dead_ids = {id(r) for r in gone}
    live = [r for r in rows if id(r) not in dead_ids]
    last_gone = max(gone, key=lambda p: p["expire"]) if gone else None
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
    """原地覆盖，不写临时文件再 rename：rename 换成新 inode 后 iCloud 上传会卡住，
    手机端一直下载到旧版本或读成 0 字节（Widget 的 raw=0 就是这么来的）。
    读到半截 JSON 的窗口由 Widget 端重试 + 本地缓存兜底。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


# Mac 侧 bird 每轮都上传成功（日志里 :00/:15/:30/:45 各一次），但手机上的 iCloud 本地
# 副本会卡在 0 字节且 downloadFileFromiCloud 变成空操作，同一个文件名永远读不出新内容。
# 每轮换一个新文件名，手机才会把它当新文件重新下载。旧固定名继续写，兼容手机上还没更新的 flow.js。
SNAPSHOT_KEEP = 24 * 3600


def snapshot_path(tail: str, now: float) -> Path:
    return ICLOUD / f"flow_{tail}_{int(now * 1000)}.json"


def prune_snapshots(tail: str, now: float) -> None:
    cutoff = int(now * 1000) - SNAPSHOT_KEEP * 1000
    for p in ICLOUD.glob(f"flow_{tail}_*.json"):
        try:
            if int(p.stem.rsplit("_", 1)[1]) < cutoff:
                p.unlink()
        except (IndexError, ValueError, OSError):
            continue


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

    # state 里只有"还在用"的包（normalize 已把失效的剔除），所以"整包从列表消失"有两种
    # 可能：真用完了，或到期作废。作废时 remain 往往还大于 0，不能当成"用完"——观测时刻
    # 必然晚于 expire，会把 Widget 上的「刚失效」永远盖成「已用完」。
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    before = {p["id"]: p for p in state.get("packages", [])}
    drained = state.get("drained_last")
    for pid, old in before.items():
        if old["remain_mb"] <= 0:
            continue
        now_remain = next((p["remain_mb"] for p in snap["packages"] if p["id"] == pid), None)
        if now_remain is not None and now_remain > 0:
            continue
        expire = old.get("expire") or ""
        if now_remain is None and expire and expire <= now_str:
            continue                                   # 到期作废，不是用完
        name = old.get("name") or pid
        key = f"done:{pid}"
        if not pushes.get(key):
            notify(bark_url, "流量包用完了", name)
            pushes[key] = today
        # 接口不给"用完时间"，只能记首次观测到归零的快照时间（15 分钟粒度）。同一轮里
        # 多个包一起归零时观测时刻相同，取到期时间较晚的（离现在更近）。
        cand = {"id": pid, "name": name, "total_mb": old.get("total_mb"), "expire": expire,
                "at": int(now)}
        if not drained or (cand["at"], cand["expire"]) >= (drained["at"], drained.get("expire") or ""):
            drained = state["drained_last"] = cand
    state["packages"] = snap["packages"]
    if drained and now - drained["at"] < 7 * 86400:
        snap["drained_last"] = drained


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
            write_json(snapshot_path(tail, now), prev)
            write_json(ICLOUD / f"flow_{tail}.json", prev)
            prune_snapshots(tail, now)
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
    snap_path = snapshot_path(tail, now)
    write_json(snap_path, snap)
    write_json(ICLOUD / f"flow_{tail}.json", snap)
    prune_snapshots(tail, now)
    save_state(tail, state)
    print(f"OK 剩余 {snap['total']['remain_mb']:.0f} MB / {len(snap['packages'])} 个包 → {snap_path}")
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
    if ICLOUD.exists():
        shutil.copy2(here / "flow.js", ICLOUD / "flow.js")
        print(f"已同步 flow.js → {ICLOUD / 'flow.js'}")
    else:
        print(f"找不到 {ICLOUD}：先在 iPhone 装 Scriptable 并打开一次，再重跑本命令", file=sys.stderr)
    subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
    subprocess.run(["launchctl", "load", str(target)], check=True)
    print(f"已加载 {target}（每 900 秒；00:00-07:00 自动跳过）")
    return 0


def self_test() -> int:
    global _query, ICLOUD
    fixture = Path(__file__).resolve().parent / "fixtures/flow_sample.json"
    snap = normalize(json.loads(fixture.read_text()), now=time.mktime((2026, 11, 20, 22, 42, 0, 0, 0, -1)))
    assert snap["ok"] is True
    assert 3900 < snap["total"]["remain_mb"] < 4100, snap["total"]["remain_mb"]
    assert abs(snap["total"]["remain_mb"] - snap["total"]["server_remain_mb"]) < 10, "我方累加与接口聚合值不符"
    assert len(snap["packages"]) == 8, [p["name"] for p in snap["packages"]]   # 一轮发放一行
    assert snap["packages"][0]["expire"] == "2026-11-21 08:06:42", "应按到期时间升序"
    rollover = [p for p in snap["packages"] if p["id"].startswith("G245489C")]
    assert len(rollover) == 2 and sum(p["total_mb"] for p in rollover) == 22147, rollover
    assert all(p["expire"] for p in snap["packages"]), "每个包都必须有有效期"
    assert snap["expired_last"] and snap["expired_last"]["expire"].startswith("2026-11-20")
    assert all("云盘" not in p["name"] for p in snap["packages"]), "定向流量不该出现"

    # 日包 planID 每次发奖都复用：同 planID、不同 validDate 必须是两行（曾经合并成 /400MB）
    def grant(valid, total, used):
        return {"flowtype": 0, "planRemianFlowInfoRes": [{
            "planID": "G305641", "planName": "统一营销流量日包-100MB",
            "validDate": f"2026-11-23 {valid}:00", "expireDate": f"2026-11-24 {valid}:00",
            "expireFlag": 0,
            "flowInfoRes": [{"totalRes": total, "usedRes": used, "remainRes": total - used, "unit": "03"}],
        }]}
    twice = {"rspBody": {"newPlanRemainQryRes": {"planRemianFlowListRes": {"planRemianFlowRes": [
        grant("14:18:42", 100, 100), grant("19:15:27", 100, 66)]}}}}
    two = normalize(twice, now=time.mktime((2026, 11, 24, 7, 0, 0, 0, 0, -1)))
    assert len(two["packages"]) == 2, two["packages"]
    assert [p["total_mb"] for p in two["packages"]] == [100.0, 100.0], two["packages"]
    assert two["total"]["remain_mb"] == 34.0, two["total"]

    day, state = 86400, {"fail": 0, "pushes": {}, "daily": []}
    for i, cum in enumerate((1000.0, 1500.0, 1800.0)):
        snapshot_and_push({"total": {"total_mb": 5000.0, "used_mb": cum, "remain_mb": 5000.0 - cum},
                           "packages": []}, state, "0000", "", now=1795000000 + i * day)
    assert [d.get("used_mb") for d in state["daily"]] == [None, 500.0, 300.0], state["daily"]
    assert snap and state["daily"][-1]["cum_mb"] == 1800.0

    pkg = {"id": "X", "name": "日包", "total_mb": 2048.0, "remain_mb": 10.0, "expire": "2026-11-23 08:00:00"}
    drain = {"fail": 0, "pushes": {}, "daily": []}
    snapshot_and_push({"total": {"total_mb": 2048.0, "used_mb": 2038.0, "remain_mb": 10.0},
                       "packages": [pkg]}, drain, "0000", "", now=1795000000)
    assert "drained_last" not in drain, "没归零不算用完"
    snapshot_and_push({"total": {"total_mb": 2048.0, "used_mb": 2048.0, "remain_mb": 0.0},
                       "packages": [dict(pkg, remain_mb=0.0)]}, drain, "0000", "", now=1795000900)
    assert drain["drained_last"]["id"] == "X" and drain["drained_last"]["at"] == 1795000900, drain

    dead = {"id": "Y", "name": "周包", "total_mb": 1024.0, "remain_mb": 300.0,
            "expire": "2026-11-18 20:00:00"}
    expired = {"fail": 0, "pushes": {}, "daily": [], "packages": [dead]}
    snapshot_and_push({"total": {"total_mb": 2048.0, "used_mb": 0.0, "remain_mb": 0.0},
                       "packages": []}, expired, "0000", "", now=1795005000)
    assert "drained_last" not in expired and not any(k.startswith("done:") for k in expired["pushes"]), \
        "到期作废不是用完"
    pair = {"fail": 0, "pushes": {}, "daily": [],
            "packages": [dict(pkg, id="A", expire="2026-11-19 08:00:00"),
                         dict(pkg, id="B", expire="2026-11-20 08:00:00")]}
    snapshot_and_push({"total": {"total_mb": 2048.0, "used_mb": 2048.0, "remain_mb": 0.0},
                       "packages": []}, pair, "0000", "", now=1795005000)
    assert pair["drained_last"]["id"] == "B", "同轮多个包归零应留到期更晚的那个"

    saved_icloud = ICLOUD
    with tempfile.TemporaryDirectory() as d:
        ICLOUD = Path(d)
        try:
            (ICLOUD / "flow_0000_100000.json").write_text('{"updated_at": 1}')
            (ICLOUD / "flow_0000_200000.json").write_text('{"updated_at": 2}')
            assert snapshot_path("0000", 3).name == "flow_0000_3000.json"
            prune_snapshots("0000", now=100 + SNAPSHOT_KEEP + 1)
            assert [p.name for p in ICLOUD.iterdir()] == ["flow_0000_200000.json"], "只清 24h 前的轮转快照"
        finally:
            ICLOUD = saved_icloud

    seq, calls = iter(["410000", "000000"]), []
    saved = (_query, cs.mint_cookie, cs.invalidate_minted, cs.note_session_ok)
    try:
        cs.mint_cookie = lambda profile: "JSESSIONID=x"
        cs.invalidate_minted = lambda phone: calls.append("invalidate")
        cs.note_session_ok = lambda phone: None
        _query = lambda cookie, tmpl, timeout: {"retCode": next(seq), "retDesc": "会话空闲超时"}
        assert fetch({"phone": "13800000000", "template": {}})["retCode"] == "000000"
        assert calls == ["invalidate"], calls
    finally:
        _query, cs.mint_cookie, cs.invalidate_minted, cs.note_session_ok = saved
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
