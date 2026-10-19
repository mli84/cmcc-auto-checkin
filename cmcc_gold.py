#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动「签到专属AI豆」话费券自动兑换脚本
==========================================

签到有礼页「签到专属AI豆兑换」商城：话费券每日限量、先到先得，
AI豆 1:1 抵扣月度话费额度（getExchangeList 的 currentFee 即 AI豆余额）。

接口（wx.10086.cn/qwhdhub/api/mark/task）：
  POST /getExchangeList   余额 + 商品列表（{"channel":"app"}）
  POST /checkStock        库存预检 {"prizeId"}（data.stock<=100 视为已抢光）
  POST /exchange          兑换 {"prizeId"}；FAILED 时 data=错误码：
                          1 内部员工不在范围；2/3 今日已抢光；其他 兑换失败

兑换策略（issue #12）：只兑话费券，按面额从高到低顶格兑——
  5元 → 2元 → 1.5元 → 1元；0.5 元档与「充值加赠券」一律不兑。
每兑一张重取余额与状态，直到没有可兑档位。商品每期限兑一次
（兑过显示 USED），AI豆不足/当日抢光/已兑过均安全跳过，重复运行幂等。

用法：
  python3 cmcc_gold.py --config config.json            # 兑换并推送
  python3 cmcc_gold.py --config config.json --dry-run  # 只查余额/档位，不兑换
  python3 cmcc_sign.py --gold --config config.json     # 挂在主签到后一起跑
退出码：0 = 正常（含无可兑档位）；1 = 异常（会话/请求失败）
"""

import argparse
import json
import logging
import sys
import time

from cmcc_sign import Config, api_headers, exchange_session, notify

log = logging.getLogger("cmcc_gold")

API = "https://wx.10086.cn/qwhdhub/api/mark/task"
LIST_URL = f"{API}/getExchangeList"
STOCK_URL = f"{API}/checkStock"
EXCHANGE_URL = f"{API}/exchange"

# 兑换优先级：面额从高到低顶格；0.5 元档与「充值加赠券」不在清单，一律不兑
TIER_NAMES = ("5元话费券", "2元话费券", "1.5元话费券", "1元话费券")
MIN_STOCK = 100        # 前端同款判定：剩余 <=100 即视为已抢光
MAX_ROUNDS = 8         # 单商品每期限兑一次，轮次上限仅作兜底


def api_post(s, referer: str, url: str, payload: dict) -> dict:
    r = s.post(url, json=payload, headers=api_headers(referer), timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_state(s, referer: str) -> tuple[float, dict, list]:
    """余额（AI豆数）与商品列表。响应异常时抛错由上层统一处理。"""
    resp = api_post(s, referer, LIST_URL, {"channel": "app"})
    data = resp.get("data") or {}
    prizes = data.get("prizes") if isinstance(data.get("prizes"), list) else []

    def num(key):
        try:
            return float(data.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return num("currentFee"), data, prizes


def pick_target(prizes: list, balance: float) -> dict | None:
    """按 TIER_NAMES 顺序选第一个可兑档：在架、库存充足、AI豆足够。"""
    by_name = {p.get("name"): p for p in prizes}
    for name in TIER_NAMES:
        p = by_name.get(name)
        if not p or p.get("status") != "WITHOUT":
            continue                       # USED 已兑过 / IN_QUEUE 发货中 / 下架
        if float(p.get("stock") or 0) <= MIN_STOCK:
            continue
        if float(p.get("cost") or 0) > balance:
            continue
        return p
    return None


def run(config_path: str, dry_run: bool = False,
        notify_result: bool = True) -> tuple[int, str]:
    """执行一轮兑换；返回 (退出码, 结果摘要)。notify_result=False 时由
    调用方（cmcc_sign.py）统一推送。"""
    cfg = Config(config_path)
    for attempt in (1, 2):
        try:
            s, referer = exchange_session(cfg)
            break
        except Exception:
            if attempt == 2:
                raise
            log.warning("会话建立失败，重试一次")

    balance, data, prizes = fetch_state(s, referer)
    lines = [f"[余额] AI豆 {balance:g}（累计 {float(data.get('totalFee') or 0):g}，"
             f"已兑 {float(data.get('exchangeFee') or 0):g}）"]
    for name in TIER_NAMES:
        p = next((x for x in prizes if x.get("name") == name), None)
        if p:
            stock = int(float(p.get("stock") or 0))
            state = {"WITHOUT": "可兑", "USED": "已兑过"}.get(p.get("status"),
                                                             p.get("status") or "?")
            if p.get("status") == "WITHOUT" and stock <= MIN_STOCK:
                state = "已抢光"       # 前端同款判定：余量 <=100 无货可兑
            lines.append(f"[档位] {name} {int(float(p['cost']))}豆 "
                         f"库存 {stock}（{state}）")

    exchanged, failed = [], []
    for _ in range(MAX_ROUNDS):
        balance, _, prizes = fetch_state(s, referer)
        target = pick_target(prizes, balance)
        if target is None:
            break
        if dry_run:
            lines.append(f"[dry-run] 将兑换 {target['name']}"
                         f"（{int(float(target['cost']))} 豆）")
            break

        stock = api_post(s, referer, STOCK_URL, {"prizeId": target["id"]})
        left = (stock.get("data") or {}).get("stock")
        if not isinstance(left, (int, float)) or left <= MIN_STOCK:
            log.info("%s 库存不足（%s），看下一档", target["name"], left)
            target["status"] = "SOLDOUT"
            continue

        resp = api_post(s, referer, EXCHANGE_URL, {"prizeId": target["id"]})
        code, err = resp.get("code"), resp.get("data")
        if code == "SUCCESS":
            exchanged.append(target["name"])
            lines.append(f"[兑换] {target['name']} 成功，去 App「我的奖品」核销")
            time.sleep(1)
            continue
        reason = {1: "内部员工不在兑换范围", 2: "今日已抢光", 3: "今日已抢光"}.get(
            err, f"未知错误码 {err}")
        lines.append(f"[兑换] {target['name']} 失败：{reason}")
        failed.append(target["name"])
        if err == 1:
            break

    if not exchanged and not failed and not dry_run:
        lines.append("[兑换] 本次无可兑档位（AI豆不足/已兑过/已抢光）")
    summary = "\n".join(lines)
    log.info("\n%s", summary)
    # 无所获的日常空跑不打扰，只有兑到或异常失败才推送
    if notify_result and not dry_run and (exchanged or failed):
        notify(cfg, "AI豆兑换通知", f"{summary}\n手机尾号 {cfg.phone[-4:]}")
    return 0, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="签到专属AI豆话费券自动兑换")
    parser.add_argument("--config", default="config.json",
                        help="配置文件路径（默认 config.json，与主脚本共用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只查余额/档位，不兑换")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        rc, _ = run(args.config, dry_run=args.dry_run)
        return rc
    except KeyboardInterrupt:
        log.info("手动中断")
        return 1
    except Exception as e:
        log.exception("执行失败: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
