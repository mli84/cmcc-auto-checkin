#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动 App「周三充值日」拼图抽奖自动化脚本
================================================

基于抓包分析的 hlwyxhdhub act-wedrecharge 活动接口（wx.10086.cn）：

  GET  /hlwyxhdhub/act-wedrecharge/1024101716?pageId=...&token=QWHDSSOD...
       → SSO 换取 HLWHD_SESSION_TOKEN 会话；进页即自动完成「每日登录」任务(+1 抽奖次数)
  POST /hlwyxhdhub/api/wedrecharge/queryActivityInfo     → 活动/访问登记（进页必调）
  POST /hlwyxhdhub/api/wedrecharge/queryPictureActInfo   → 拼图状态（drawTimes 剩余次数、
                                                          activeIndex 当前拼图、pictureList 碎片）
  POST /hlwyxhdhub/api/wedrecharge/taskList              → 任务清单（status: 0 未完成 / 2 已完成）
  POST /hlwyxhdhub/api/wedrecharge/finishTask            → 任务完成上报 {"taskId":"<id>"}，+1 次
  POST /hlwyxhdhub/api/wedrecharge/drawPicture           → 抽一次 {"round":"dp<N>"}，随机得一片拼图
  POST /hlwyxhdhub/api/wedrecharge/receiveNew            → 集齐领取 {"round":"r6/r5/r4"}，得话费券

玩法：抽拼图集齐当前档位的全部碎片解锁话费券（dp1=95折 4片 → dp2=9折 6片 →
dp3=8折 9片），依次解锁不可跳级；碎片由抽奖随机获得，每次抽奖消耗 1 次机会。

任务处理策略（taskList + finishTask 抓包实测）：
  - dlhd 每日登录：进页自动发放，无需操作；
  - llym「参与签到并浏览5秒」：jt 握手（getJtToken 签发凭证 → 携凭证 SSO
    访问签到页，服务端在跳转时登记完成）；
  - 其余浏览/查询类（ll_yxr/ll_spr/ll_yd/ll_sp/cc_hf/cc_zd/ll_hf）：完成
    上报接口携带 taskId 即可，8 项中 7 项实测有效；
  - yqyh 邀请助力 / czhf 充值满10元：需真实受邀者/真实消费，不代做，只报状态。

会话与凭证完全复用 cmcc_sign.exchange_session()（jwt 自续期缓存同样生效，
同一 jwt 跨 hub 通用，本活动落 HLWHD_SESSION_TOKEN）。

用法：
  python3 cmcc_wed.py                    # 进页 + 试做任务 + 抽光当日次数
  python3 cmcc_wed.py --dry-run          # 只查次数/拼图进度/任务状态，零消耗
  python3 cmcc_wed.py --config config2.json   # 多账号
退出码：0 = 正常；1 = 异常（便于 CI 判断）
"""

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

from cmcc_sign import BASE, Config, api_headers, assert_safe_url, exchange_session, notify

# 与抓包一致的活动页地址（pageId/channelId 为活动公开配置）
WED_ACT_URL = ("https://wx.10086.cn/hlwyxhdhub/act-wedrecharge/1024101716"
               "?pageId=1849008675699650560&channelId=P00000093619")
API_WED = BASE + "/hlwyxhdhub/api/wedrecharge"

# 拼图档位名（dpN 与 queryPictureActInfo.activeIndex / drawPicture.round 对应）
TIER_NAMES = {"dp1": "95折话费券", "dp2": "9折话费券", "dp3": "8折话费券"}

# 集齐后的领取映射（receiveNew 的 round 与 dpN 不同源，取自前端 H 表：
# [{p22,95折,r6},{p23,9折,r5},{p33,8折,r4}]），拼图序号 → 领取 round
PUZZLE_RECEIVE = {1: "r6", 2: "r5", 3: "r4"}
RECEIVE_LABELS = {"r6": "95折话费券", "r5": "9折话费券", "r4": "8折话费券"}

# 代做采用黑名单制：除以下三类外全部自动尝试（含服务端动态新增的任务），
# 每项 +1 抽奖次数。失败会如实报告，不影响其他任务。
MANUAL_SKIP = {
    "yqyh": "需邀请真实用户登录助力",
    "czhf": "需真实充值满10元（涉及消费，不做自动化）",
    "llym": "需在 App 内真实浏览签到页（每日1次/月4次，点一下即可）",
    "dlhd": "进页自动完成",
}

# 浏览 5 秒的语义来自 llym 的任务名「参与签到并浏览5秒」
BROWSE_SECONDS = 5

log = logging.getLogger("cmcc_wed")


def wed_api(s: requests.Session, referer: str, path: str, payload: dict | None = None) -> dict:
    """调 wedrecharge API。本 hub 的成功码是字符串 "0"（区别于 qwhdhub 的 SUCCESS）。"""
    r = s.post(f"{API_WED}/{path}", json=payload or {}, headers=api_headers(referer), timeout=30)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") != "0":
        raise RuntimeError(f"{path} 失败: {resp.get('code')} {resp.get('msg')}")
    return resp.get("data") or {}


def puzzle_lines(state: dict) -> list[str]:
    """把 queryPictureActInfo 的 pictureList 渲染成可读进度行。

    碎片值实测为计数：无="0"，持有="1"，重复抽到会累加（"2"表示该片有多余副本），
    因此按 值>=1 统计持有数。
    """
    pics = state.get("pictureList") or {}
    lines = []
    for idx in sorted(pics, key=lambda x: int(x)):
        pieces = pics[idx] or {}
        held = {k: int(v) for k, v in pieces.items() if str(v).isdigit() and int(v) >= 1}
        have, total = len(held), len(pieces)
        dup = sum(n - 1 for n in held.values() if n > 1)
        tier = TIER_NAMES.get(f"dp{idx}", f"dp{idx}")
        mark = " ✓已集齐" if pieces and have == total else ""
        if dup:
            mark += f"（重复抽到x{dup}）"
        lines.append(f"拼图{idx}（{tier}）碎片 {have}/{total}{mark}")
    return lines


def quota_left(t: dict) -> int | None:
    """任务剩余可做次数（月度配额 requireTimes/completedTimes，服务端下发）。

    每日次数由服务端 status 控制（status=2 即今日不可再做），这里只看月度余量；
    无配额字段的任务返回 None（不设限）。
    """
    req, done = t.get("requireTimes"), t.get("completedTimes") or 0
    if isinstance(req, int) and req > 0:
        return max(0, req - done)
    return None


def task_lines(tasks: list[dict]) -> list[str]:
    """任务状态行：已完成打勾；可代做的标注；不可代做的给原因。"""
    lines = []
    for t in sorted(tasks, key=lambda x: x.get("order", 99)):
        tid = t.get("taskId")
        title = t.get("title", tid)
        left = quota_left(t)
        done = t.get("status") == 2 or left == 0
        add = t.get("addDrawTimes")
        quota = f"，月度{t.get('completedTimes') or 0}/{t['requireTimes']}" if left is not None else ""
        if done:
            # status=2 语义是"今日不可再做"：已完成（明日再来）或未开放（周日开放）
            note = t.get("buttontxt") or "已完成"
            lines.append(f"✓ {title}（+{add}次{quota}，{note}）")
        elif tid in MANUAL_SKIP:
            lines.append(f"· {title}（+{add}次{quota}，{MANUAL_SKIP[tid]}）")
        else:
            lines.append(f"→ {title}（+{add}次{quota}，脚本代做）")
    return lines


def open_handshake_wed(cfg: Config, jump: str, jt_token: str) -> bool:
    """开放平台完成链：携 jtToken 为目标页换会话 → getOneTaskInfo 取 cToken
    → openFinish（先立即试，失败再等 scanTime 重试）。

    会话必须是目标页自己的（App 内点「去完成」时 WebView 打开目标页并
    经 SSO 落对应令牌：签到页→QWHD、充值日页→HLWHD），否则兑换返回
    FAILED 且 msg 为会话令牌。"""
    sep = "&" if "?" in jump else "?"
    s2, ref2 = None, None
    last_err = None
    for wait in (0, 3):  # 换票偶发限频，退避重试一次
        if wait:
            time.sleep(wait)
        try:
            s2, ref2 = exchange_session(cfg, act_url=f"{jump}{sep}jtToken={jt_token}")
            break
        except RuntimeError as e:
            last_err = e
    if s2 is None:
        log.warning("[任务] 目标页会话换票失败: %s", last_err)
        return False

    def get_ct() -> tuple[str, dict]:
        info = s2.post("https://wx.10086.cn/hlwyxhdhub/api/open/_pub/task/getOneTaskInfo",
                       json={"jtToken": jt_token}, headers=api_headers(ref2), timeout=30)
        d = info.json().get("data") or {}
        return d.get("cToken") or "", d

    ct, d = get_ct()
    if not ct:
        log.info("[任务] getOneTaskInfo 未取得 cToken")
        return False
    fin = s2.post("https://wx.10086.cn/hlwyxhdhub/api/open/_pub/task/openFinish",
                  json={"cToken": ct}, headers=api_headers(ref2), timeout=30)
    if fin.json().get("code") == "SUCCESS":
        return True
    # 回退：换新 cToken 等满 scanTime 再上报
    ct, d = get_ct()
    if not ct:
        return False
    time.sleep(int(d.get("scanTime") or 5) + 1)
    fin = s2.post("https://wx.10086.cn/hlwyxhdhub/api/open/_pub/task/openFinish",
                  json={"cToken": ct}, headers=api_headers(ref2), timeout=30)
    return fin.json().get("code") == "SUCCESS"


def finish_tasks(cfg: Config, s: requests.Session, referer: str,
                 tasks: list[dict], lines: list[str]):
    """对可代做的未完成任务逐一完成，每次 +1 抽奖次数。

    黑名单制：除邀请/充值/每日登录外全部自动尝试，服务端动态新增的
    任务 ID 无需维护清单。自适应两级：
    1. finishTask 直接上报（零成本，查询/浏览类任务在此即成功）；
    2. 失败再走 jt 握手完整链：getJtToken 签发凭证 → 携凭证换目标页会话
       → getOneTaskInfo 取 cToken → 停留 scanTime → openFinish；
    3. 握手后复查状态，仍未登记则最后再补一次上报。
    """
    todo = [t for t in tasks
            if t.get("taskId") not in MANUAL_SKIP
            and t.get("status") != 2
            and quota_left(t) != 0]   # 月度配额用尽的不空发
    if not todo:
        return

    def task_status(tid):
        return next((x for x in wed_api(s, referer, "taskList")
                     if x.get("taskId") == tid), {})

    def finish_once(tid, hdr_referer=None):
        """单次上报（160030 网络异常退避重试），返回响应或抛异常。"""
        resp, last_err = None, None
        for wait in (0, 3, 8):
            if wait:
                time.sleep(wait)
            try:
                h = api_headers(hdr_referer) if hdr_referer else api_headers(referer)
                resp = wed_api(s, referer, "finishTask", {"taskId": tid})
                break
            except RuntimeError as e:
                last_err = e
                if "160030" not in str(e):
                    raise
        if resp is None:
            raise last_err
        return resp

    for t in todo:
        tid = t.get("taskId")
        title = t.get("title", tid)
        jump = t.get("jumpUrl") or ""
        done = False

        # ① 快路径：直接上报
        try:
            resp = finish_once(tid)
            add = (resp.get("data") or {}).get("addDrawTimes")
            lines.append(f"[任务] {title} 代做成功" + (f"，+{add} 次" if add else ""))
            done = True
        except Exception as e:
            log.info("[任务] %s 直接上报未过（%s），改走 jt 握手", title, e)

        # ② jt 握手完整链
        if not done:
            hs_ok = False
            try:
                r = s.post(f"{API_WED}/getJtToken", json={"taskId": tid},
                           headers=api_headers(referer), timeout=30)
                jt = r.json().get("data")
                if isinstance(jt, str) and jt:
                    hs_ok = open_handshake_wed(cfg, jump, jt)
                    log.info("[任务] jt 握手%s: %s", "完成" if hs_ok else "未通过", title)
            except Exception as e:
                log.warning("[任务] %s jt 握手异常: %s", title, e)
            if hs_ok:
                try:
                    cur = task_status(tid)
                    # 状态刷新偶有延迟：先立即查，未完成再等 1 秒复查
                    if cur.get("status") != 2:
                        time.sleep(1)
                        cur = task_status(tid)
                    if cur.get("status") == 2:
                        lines.append(f"[任务] {title} 代做成功（+1 次）")
                        done = True
                except Exception:
                    pass

        # ③ 握手可能已登记但状态延迟，最后补一次上报
        if not done:
            if urlsplit(jump).hostname == "wx.10086.cn":
                try:
                    assert_safe_url(jump, allowed_hosts={"wx.10086.cn"})
                    s.get(jump, timeout=30)
                except Exception:
                    pass
            try:
                resp = finish_once(tid)
                add = (resp.get("data") or {}).get("addDrawTimes")
                lines.append(f"[任务] {title} 代做成功" + (f"，+{add} 次" if add else ""))
                done = True
            except Exception as e:
                lines.append(f"[任务] {title} 上报失败: {e}")

        if not done:
            time.sleep(random.uniform(1, 2))


def puzzle_complete(pieces: dict) -> bool:
    """当前拼图是否已集齐（所有碎片值 >= 1）。"""
    return bool(pieces) and all(str(v).isdigit() and int(v) >= 1 for v in pieces.values())


def receive_ready(s: requests.Session, referer: str, state: dict, lines: list[str]):
    """集齐的档位自动领取 receiveNew（奖励月底清零，默认代领）。

    响应分三类渲染：成功展示券名与使用条件；"已领取"是正常状态不算错误；
    其余 code 才按失败报告。卡号等敏感字段不进日志。
    """
    pics = state.get("pictureList") or {}
    for idx_s in sorted(pics, key=lambda x: int(x)):
        if not puzzle_complete(pics[idx_s] or {}):
            continue
        rnd = PUZZLE_RECEIVE.get(int(idx_s))
        if not rnd:
            continue
        label = RECEIVE_LABELS.get(rnd, rnd)
        r = s.post(f"{API_WED}/receiveNew", json={"round": rnd},
                   headers=api_headers(referer), timeout=30)
        try:
            resp = r.json()
        except ValueError:
            lines.append(f"[领取] {label} 领取失败: 响应异常")
            continue
        msg = resp.get("msg") or ""
        if resp.get("code") == "0":
            prizes = (resp.get("data") or {}).get("actPrizes") or []
            names = "、".join(filter(None, (p.get("prizeName") for p in prizes)))
            sub = "、".join(filter(None, (p.get("subTitle") for p in prizes)))
            lines.append(f"[领取] {label} 领取成功（{names or label}"
                         + (f"，{sub}" if sub else "") + "）")
        elif "已领取" in msg:
            lines.append(f"[领取] {label} 已领取过")
        else:
            lines.append(f"[领取] {label} 领取失败: {msg or resp.get('code')}")
        time.sleep(random.uniform(1, 2))


def draw_all(s: requests.Session, referer: str, lines: list[str], max_draws: int = 30):
    """抽光当前所有次数：每抽后重查状态，次数用尽或连续异常即停。

    当前拼图集齐但次数有剩时主动收手——解锁下一档的机制未抓包确认前，
    不把机会浪费在已完成的拼图上。
    """
    total = 0
    for _ in range(max_draws):
        state = wed_api(s, referer, "queryPictureActInfo")
        times = int(state.get("drawTimes") or 0)
        if times <= 0:
            break
        active = int(state.get("activeIndex") or 1)
        pieces = (state.get("pictureList") or {}).get(str(active)) or {}
        if puzzle_complete(pieces):
            lines.append(f"[抽奖] 拼图{active}已集齐但还剩 {times} 次，暂存不用（解锁机制待观察）")
            break
        r = s.post(f"{API_WED}/drawPicture", json={"round": f"dp{active}"},
                   headers=api_headers(referer), timeout=30)
        resp = r.json()
        if resp.get("code") != "0":
            lines.append(f"[抽奖] 停止: {resp.get('code')} {resp.get('msg')}")
            break
        prize = (resp.get("data") or {}).get("prize") or {}
        total += 1
        log.info("[抽奖] dp%s 第%d抽 → %s", active, total, prize.get("prizeId"))
        time.sleep(random.uniform(1, 2))
    lines.append(f"[抽奖] 本次共抽 {total} 次")


def run(cfg: Config, dry_run: bool) -> list[str]:
    """主流程，返回报告行（不带前缀，由调用方/本模块 main 统一加）。"""
    # SSO 换 hlwyxhdhub 会话；exchange_session 内部会访问活动页（即"每日登录"动作本身）
    s, referer = exchange_session(cfg, act_url=WED_ACT_URL)

    wed_api(s, referer, "queryActivityInfo")  # 进页登记
    state = wed_api(s, referer, "queryPictureActInfo")
    tasks = wed_api(s, referer, "taskList")

    lines = []
    active = int(state.get("activeIndex") or 1)
    tier = TIER_NAMES.get(f"dp{active}", f"dp{active}")
    lines.append(f"剩余抽奖次数: {state.get('drawTimes')}，当前拼图: {tier}（拼图{active}）")
    lines += puzzle_lines(state)
    lines += task_lines(tasks)

    if not dry_run:
        finish_tasks(cfg, s, referer, tasks, lines)
        draw_all(s, referer, lines)

    # 终态
    final = wed_api(s, referer, "queryPictureActInfo")
    lines.append(f"终态: 剩余 {final.get('drawTimes')} 次")
    lines += puzzle_lines(final)

    # 集齐即领（奖励月底清零）：dry-run 只报「可领」，正式运行代领
    completed = sorted((idx for idx, pieces in (final.get("pictureList") or {}).items()
                        if puzzle_complete(pieces or {})), key=int)
    if completed and dry_run:
        tiers = "、".join(f"拼图{i}（{RECEIVE_LABELS.get(PUZZLE_RECEIVE.get(int(i), '?'), '?')}）"
                          for i in completed)
        lines.append(f"[领取] {tiers} 已集齐，正式运行将自动领取")
    elif completed:
        receive_ready(s, referer, final, lines)

    if dry_run:
        lines.append("[dry-run] 未做任务、未抽奖")
    return lines


# ---------------------------------------------------------------- 秒杀

# drawPrize 实测：场次未开时返回 160001「活动还未开始」（时间条件错误而非参数
# 错误），说明这就是轮换抽奖/秒杀端点。无库存等终态码命中即停，
# 其余（网络限流等）继续打满预算。
SEK_STOP_CODES = {"160002"}          # 无库存
SEK_SUCCESS_CODE = "0"


def server_time(s: requests.Session, referer: str) -> tuple[float, dict]:
    """取服务器时钟与场次信息（queryDrawPrizeInfo 带 currentTime/rdStatus）。"""
    d = wed_api(s, referer, "queryDrawPrizeInfo")
    return d["currentTime"] / 1000, d


def seckill_run(cfg: Config, at: str, lead: float, interval: float,
                max_attempts: int, dry_run: bool, force: bool = False) -> list[str]:
    """周三充值日场次抢购：等服务器时间到点，快速连发 drawPrize 直到命中或打完预算。

    实测 drawPrize 未开场返回 160001「活动还未开始」，开场后 code=0 且
    data.actPrizes 带券信息。每发响应完整留痕（log），首场若落空可凭日志修正。

    长时间等待的会话保活：HLWHD 会话 Cookie 仅 30 分钟有效，等待期间校时
    失败自动重建会话，并在开火前约 2 分钟强制刷新一次，保证开火窗口内
    会话有效。
    """
    s, referer = exchange_session(cfg, act_url=WED_ACT_URL)

    def fresh_session():
        nonlocal s, referer
        s, referer = exchange_session(cfg, act_url=WED_ACT_URL)

    now_sv, info = server_time(s, referer)
    skew_ms = (now_sv - time.time()) * 1000
    now_dt = datetime.fromtimestamp(now_sv)
    weekday_cn = "一二三四五六日"[now_dt.weekday()]

    # 场次与奖品（rec_login 下发；seckillTime 缺省 [8,12,18]）
    try:
        rec = s.get(f"{API_WED}/_pub/rec_login", timeout=15).json().get("data") or {}
    except Exception:
        rec = {}
    # seckillTime 服务端未下发时（实测为 null）不采信前端兜底的 [8,12,18]，
    # 按 App 实际显示只取 08:00 一场
    sk_raw = rec.get("seckillTime")
    if sk_raw:
        sk_hours = [int(h) for h in sk_raw]
        sess_note = "服务端下发"
    else:
        sk_hours = [8]
        sess_note = "服务端未下发，按 App 显示取 08:00"
    nfos = rec.get("Seckillnfos") or []

    lines = [f"服务器时间 {now_dt.strftime('%m-%d %H:%M:%S')}，本机时钟偏移 {skew_ms:+.0f}ms"]
    sess_lines = []
    for h in sk_hours:
        t = now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        sess_lines.append(f"  {h:02d}:00" + ("（已过）" if t.timestamp() < now_sv else ""))
    lines.append(f"共 {len(sk_hours)} 个场次（{sess_note}）:\n" + "\n".join(sess_lines))
    if nfos:
        lines.append("秒杀奖品: " + "；".join(
            f"{p.get('name','')}" + (f"（{p['subTitle']}）" if p.get("subTitle") else "")
            for p in nfos))

    # 选定目标场次：--at 优先，否则取最近一个未开始的场次
    if at:
        hh, mm, ss = (int(x) for x in at.split(":"))
        target = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        src = f"--at {at}"
    else:
        future = [h for h in sk_hours
                  if now_dt.replace(hour=h, minute=0, second=0, microsecond=0).timestamp() >= now_sv]
        if not future:
            lines.append("今日场次已全部结束，明日再跑（或 --at 指定场次）")
            return lines
        target = now_dt.replace(hour=future[0], minute=0, second=0, microsecond=0)
        src = f"场次 {future[0]:02d}:00"
    if target.timestamp() < now_sv - 3600:  # 已过目标 1 小时以上则等明天
        target += timedelta(days=1)
        src += "+1天"

    # 场次在周三（活动规则），非周三等待只会到点空打
    if target.weekday() != 2 and not force:
        lines.append(f"目标日 {target.strftime('%m-%d')} 为周"
                     f"{'一二三四五六日'[target.weekday()]}，非周三；"
                     "确认活动改期可加 --force 强制等待")
        return lines

    away = int(target.timestamp() - now_sv)
    lines.append(f"选定目标：{target.strftime('%m-%d %H:%M:%S')}（{src}，周{weekday_cn}），"
                 f"距开始 {away} 秒；提前 {lead}s 开火，间隔 {interval}s，预算 {max_attempts} 发")

    for ln in lines:   # 启动信息实时输出，等待期间控制台不再静默
        log.info(ln)

    if dry_run:
        lines.append("[dry-run] 不发抢购请求")
        return lines

    # 等待到点（含 lead 提前量）。分段：
    # - 距开火 >150s：每 30s 校时一次（会话失效则重建），并按服务器时钟校准截止
    # - 150s ≥ 距开火 >30s：强制重建会话（Cookie 有效期 30 分钟），随后静候
    # - ≤30s：0.2s 步进贴等待，到点即开火
    deadline_local = time.time() + max(0.0, target.timestamp() - lead - now_sv)
    refreshed = False
    while True:
        remain = deadline_local - time.time()
        if remain <= 0:
            break
        if refreshed and remain > 30:
            time.sleep(min(remain - 30, 20))
            continue
        if remain > 150:
            time.sleep(min(30, remain - 150))
            if not refreshed:
                try:
                    now_sv, _ = server_time(s, referer)
                    deadline_local = time.time() + max(0.0, target.timestamp() - lead - now_sv)
                except Exception:
                    fresh_session()
            continue
        if not refreshed and remain > 30:
            fresh_session()   # 开火前刷新会话，保证到点时 Cookie 有效
            refreshed = True
            continue
        time.sleep(min(0.2, remain))

    last, fired = "", 0
    for i in range(1, max_attempts + 1):
        fired = i
        t0 = time.monotonic()
        try:
            r = s.post(f"{API_WED}/drawPrize", json={}, headers=api_headers(referer), timeout=10)
            resp = r.json()
        except Exception as e:
            resp = {"code": "EXC", "msg": str(e)}
        code, msg = str(resp.get("code")), resp.get("msg") or ""
        log.info("[秒杀] 第%d发: %s %s", i, code, msg)
        if code == SEK_SUCCESS_CODE:
            prizes = (resp.get("data") or {}).get("actPrizes") or []
            for p in prizes:
                lines.append(f"[秒杀] 抢中: {p.get('prizeName') or p.get('shortName') or ''}"
                             + (f"（{p['subTitle']}）" if p.get("subTitle") else ""))
            break
        last = f"{code} {msg}"
        if code in SEK_STOP_CODES:
            lines.append(f"[秒杀] 停止: {last}")
            break
        # 按间隔节奏连发（扣除本次请求耗时）
        remain = interval - (time.monotonic() - t0)
        if remain > 0:
            time.sleep(remain)
    else:
        lines.append(f"[秒杀] {max_attempts} 发打完未中，最后响应: {last}")

    lines.append(f"[秒杀] 共尝试 {fired} 发")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="中国移动「周三充值日」拼图抽奖自动脚本")
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json，与 cmcc_sign.py 共用）")
    parser.add_argument("--dry-run", action="store_true", help="只查状态，不做任务不抽奖")
    parser.add_argument("--delay", type=int, default=0, metavar="N", help="启动前随机延迟 0~N 秒")
    parser.add_argument("--seckill", action="store_true",
                        help="秒杀模式：等到目标场次时间连发 drawPrize（周三 8 点场）")
    parser.add_argument("--at", default=None, help="秒杀场次时间（服务器时间，默认自动选最近未开始场次）")
    parser.add_argument("--force", action="store_true", help="目标日非周三也强制等待开抢")
    parser.add_argument("--lead", type=float, default=0.4, help="提前开火秒数（默认 0.4）")
    parser.add_argument("--interval", type=float, default=0.35, help="连发间隔秒（默认 0.35）")
    parser.add_argument("--max-attempts", type=int, default=120, help="最多连发次数（默认 120）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.delay > 0:
        wait = random.uniform(0, args.delay)
        log.info("随机延迟 %.0f 秒后开始...", wait)
        time.sleep(wait)

    cfg = Config(args.config)
    if err := cfg.validate():
        log.error("%s\n参考 config.example.json 填写，或设置对应环境变量", err)
        return 1

    try:
        if args.seckill:
            lines = seckill_run(cfg, at=args.at, lead=args.lead,
                                interval=args.interval, max_attempts=args.max_attempts,
                                dry_run=args.dry_run, force=args.force)
            title = "周三充值日秒杀"
        else:
            lines = run(cfg, dry_run=args.dry_run)
            title = "周三充值日拼图"
        summary = "\n".join(lines)
        log.info("\n%s", summary)
        notify(cfg, title, f"{summary}\n手机尾号 {cfg.phone[-4:]}")
        return 0
    except KeyboardInterrupt:
        log.info("手动中断退出")
        return 130
    except Exception as e:
        log.exception("执行失败: %s", e)
        notify(cfg, "周三充值日异常", f"{e}\n手机尾号 {cfg.phone[-4:] if cfg.phone else '????'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
