#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
辽宁移动「充值先领券」自动抽奖（activityId 1025093012）
================================================================

活动：每日进活动页抽奖 1 次，随机中充值话费券（9~9.95 折折扣券/满减券/兑换券等）；
     任务可加抽奖机会；邀请好友每月额外 1 次 10-1 元话费满减券机会。
     仅限辽宁移动实名个人用户。活动页：
     https://wx.10086.cn/qwhdhub/goldeneggs/1025093012

用法（在本目录执行，或任意目录用绝对路径）：
  python3 liaoning.py                      # 每日自动抽奖
  python3 liaoning.py --dry-run            # 只建立会话查状态，不抽奖

凭证：根目录 config.json 内含 jwt（主凭证，服务端每次登录自动续签，
     脚本会把最新 jwt 回写缓存）与 app_token（备用，jwt 失效时自动兜底）。
     两者的关系与主脚本一致：jwt 长期有效，app_token 仅首次引导用。

任务缺口（TODO）：
  任务清单里有「参与东北"超"级酷」（浏览型，startBrowse/finish，完成后
  +1 抽奖机会），报文细节待抓包确认后补 run_tasks()；邀请类任务需真实
  好友，不做。
"""

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cmcc_sign import (  # noqa: E402
    BASE, api_headers, exchange_session, load_jwt_cache, notify, save_jwt_cache,
)

ACT_URL = f"{BASE}/qwhdhub/goldeneggs/1025093012?A_C_CODE=RLnnD8f2p6"
BACK_URL = ("https://wx.10086.cn/qwhdhub/goldeneggs/1025093012"
            "?A_C_CODE=RLnnD8f2p6&channelId=P00000003143#/")
ROOT = Path(__file__).resolve().parent.parent   # 项目根目录
DEFAULT_CONFIG = ROOT / "config.json"           # 与其他脚本一致：默认根目录 config.json

log = logging.getLogger("liaoning")


class LNConfig:
    """最小配置桩：对接 cmcc_sign 的会话/凭证/通知工具。

    exchange_session 按 cfg.phone 读写 jwt 缓存（.cmcc_jwt_cache_<尾4>.json），
    jwt 失效时回落 cfg.app_token 引导，两份凭证都在 config.json 里。
    """

    def __init__(self, c: dict):
        self.phone = c["phone"]
        self.app_token = c.get("app_token", "")
        self.act_url = ACT_URL
        self.province_code = c.get("province_code", "240")
        self.city_code = c.get("city_code", "0429")
        self.carrier_operator = c.get("carrier_operator", "002")
        self.app_version_code = c.get("app_version_code", "12.0.3")
        self.bark_url = c.get("bark_url", "")
        self.serverchan_sendkey = c.get("serverchan_sendkey", "")
        self.jwt_seed = c.get("jwt", "")


def ensure_session(cfg: LNConfig):
    """首次运行用 config.json 里的 jwt 播种缓存，之后走常规续期。"""
    if not load_jwt_cache(cfg) and cfg.jwt_seed:
        save_jwt_cache(cfg, cfg.jwt_seed)
        log.info("已用 config.json 的 jwt 播种凭证缓存")
    return exchange_session(cfg, act_url=ACT_URL)


BACK_URL = ("https://wx.10086.cn/qwhdhub/goldeneggs/1025093012"
            "?A_C_CODE=RLnnD8f2p6&channelId=P00000003143#/")


def run_tasks(s, referer, do: bool) -> list:
    """任务（task/list；browse 类型 GET startBrowse 即登记完成——302 到目标页、
    无需 finish，抓包实测，任务 url 须原样携带 dwzCode/targetUrl 参数）：

    - type=browse：GET 任务 url 并跟随 302 访问目标页，停留后复查
      taskStage=DONE，完成加抽奖机会；
    - type=share_view：邀请类，需真实好友，跳过。
    do=False（dry-run）时只列状态，不做任何操作。返回结果行。
    """
    out = []
    r = s.get(f"{BASE}/qwhdhub/task/list", params={"backUrl": BACK_URL},
              headers=api_headers(referer), timeout=30)
    tasks = r.json().get("data") or []
    for t in tasks:
        name = t.get("name") or t.get("taskId")
        stage = t.get("taskStage")
        ttype = t.get("type") or ""
        turl = t.get("url") or ""
        add = t.get("earnOpportunity") or 1
        if stage == "DONE":
            out.append(f"✓ [任务] {name} 已完成")
        elif stage != "UNDO":
            out.append(f"· [任务] {name} 今日不可再做")
        elif ttype == "browse" and turl.startswith("/qwhdhub/task/startBrowse"):
            if do:
                try:
                    s.get(BASE + turl, headers=api_headers(referer), timeout=30,
                          allow_redirects=True)
                    time.sleep(6)
                    r2 = s.get(f"{BASE}/qwhdhub/task/list", params={"backUrl": BACK_URL},
                               headers=api_headers(referer), timeout=30)
                    cur = next((x for x in r2.json().get("data") or []
                                if x.get("taskId") == t.get("taskId")), {})
                    if cur.get("taskStage") == "DONE":
                        out.append(f"[任务] {name} 完成（+{add} 抽奖机会）")
                    else:
                        out.append(f"[任务] {name} 未生效")
                except Exception as e:
                    out.append(f"[任务] {name} 失败: {e}")
            else:
                out.append(f"→ [任务] {name}（+{add} 次，正式运行将自动完成）")
        else:
            out.append(f"· [任务] {name}（{ttype or '未知类型'}）需真实操作，跳过")
        time.sleep(random.uniform(0.3, 0.6))
    return out


def draw(s, referer) -> str:
    """每日抽奖：GET lotterySafely 一发直接开奖。

    次数用完时返回 code=DO_TASK，data 为当前可做任务（服务端说明：browse 类
    直接访问其 url 即跳转完成，hvb 类对其 url 发 ajax）。邀请类（share_view）
    需真实好友，跳过。"""
    r = s.get(f"{BASE}/qwhdhub/lottery/lotterySafely",
              headers=api_headers(referer), timeout=30)
    try:
        resp = r.json()
    except ValueError:
        return f"[抽奖] 响应异常（HTTP {r.status_code}）：该活动仅限辽宁移动号码参与"

    if resp.get("code") == "DO_TASK":
        task = resp.get("data") or {}
        ttype = task.get("type") or ""
        turl = task.get("url") or ""
        if ttype == "share_view" or "share" in ttype:
            # 服务端按 UTC 计天：每日机会在北京时间 8 点刷新（实测 winAt 差 8h）
            now_bj = datetime.now(timezone(timedelta(hours=8)))
            tip = ("（服务端按 UTC 计天，北京时间 8 点后刷新每日机会）"
                   if now_bj.hour < 8 else "")
            return f"[抽奖] 免费机会已用完；剩余任务为邀请类（需真实好友），跳过{tip}"
        if turl:
            # browse：GET 访问即跳转完成；hvb：POST ajax。完成后重抽一次
            log.info("[任务] DO_TASK %s（%s），尝试完成", task.get("name"), ttype)
            try:
                if ttype == "hvb":
                    s.post(BASE + turl, json={}, headers=api_headers(referer), timeout=30)
                else:
                    s.get(BASE + turl, headers=api_headers(referer), timeout=30)
                time.sleep(1)
                r = s.get(f"{BASE}/qwhdhub/lottery/lotterySafely",
                          headers=api_headers(referer), timeout=30)
                resp = r.json()
            except (ValueError, Exception) as e:
                return f"[抽奖] 任务尝试失败: {e}"

    if resp.get("code") != "SUCCESS":
        return f"[抽奖] 未完成: {resp.get('code')} {resp.get('msg')}"
    d = resp.get("data") or {}
    prize = d.get("prizeName") or ""
    if d.get("prizeValue"):
        unit = {"FLOW": "MB", "FEE": "元"}.get(d.get("prizeCategory"), "")
        prize += f"（{d['prizeValue']}{unit}）"
    if not prize:
        return f"[抽奖] 未中奖（{resp.get('msg')}）"
    return f"[抽奖] 中得 {prize}"


def main() -> int:
    ap = argparse.ArgumentParser(description="辽宁移动「充值先领券」自动抽奖")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="默认根目录 config.json")
    ap.add_argument("--dry-run", action="store_true", help="只建立会话，不抽奖")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cfg = LNConfig(raw)

    s, referer = ensure_session(cfg)
    log.info("会话建立成功")

    # 任务：dry-run 只列状态，正式运行为 UNDO 的 browse 任务自动完成
    task_lines = run_tasks(s, referer, do=not args.dry_run)

    line = "[dry-run] 未抽奖"
    if not args.dry_run:
        line = draw(s, referer)

    summary = "\n".join(task_lines + [line])
    log.info("\n%s", summary)
    notify(cfg, "辽宁充值领券", f"{summary}\n手机尾号 {cfg.phone[-4:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
