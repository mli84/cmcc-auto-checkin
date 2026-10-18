#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动「AI豆苗养成记」自动任务脚本
=====================================

活动：移动爱购 AI豆苗养成记（activityId 17692），做任务赚阳光值，
      阳光值可在活动页兑换影音会员、外卖券等权益。

接口（dev.coc.10086.cn/coc3/coc3-market-activity）：
  POST /api/activity/slgCredits/getSlgCreditsHomePage   活动页状态/待领阳光值
  POST /api/activity/slgCredits/queryGrowthValue        当前阳光值
  POST /api/activity/slgCredits/getGrowthBubble         领取一条待领阳光值
  POST /cmpp-manage/task/getTaskSignInfo                签到任务信息
  POST /cmpp-manage/task/accumulateSign                 每日签到（+10 阳光值）
  POST /cmpp-manage/task/getGrowthTaskList              任务清单
  POST /cmpp-manage/task/visit                          浏览/直播类任务完成上报
  POST /cmpp-manage/task/share                          分享类任务完成上报

鉴权：活动接口只认请求头 `phone`——权益市场统一登录（userLoginV2）下发的
      加密手机号 token，不是用手机号现场算出来的。App 内打开活动页后，
      从任意 dev.coc.10086.cn 请求头复制 `phone` 值填入配置即可，长期有效；
      不需要 cookie，也不需要 wx.10086.cn 的 jwt。

可自动完成：
  - 每日签到（+10 阳光值，按本地状态每天一次）；
  - 3 个每周浏览任务（移动AI你/幸运三日签/1豆有好礼，各 +10）；
  - 每日看移动爱购直播（+10）；
  - 每日分享好友（+10）；
  - 任务完成后自动领取阳光值气泡。
不代做：
  - 订购铂金/畅看/生活会员（真实消费）；
  - 邀请好友助力（真实好友）；
  - 关注公众号/微博（需真实关注；当前也常显示“任务不在进行中”）。

用法：
  python3 cmcc_seed.py --config config2.json
  python3 cmcc_seed.py --config config2.json --dry-run
  python3 cmcc_seed.py --config config2.json --no-sign
退出码：0 = 正常/跳过；1 = 异常（便于 CI 判断）
"""

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from cmcc_sign import notify

ROOT = Path(__file__).resolve().parent
BASE = "https://dev.coc.10086.cn/coc3/coc3-market-activity"
PAGE_URL = ("https://dev.coc.10086.cn/coc3/canvas/rightsmarket-h5-canvas"
            "/online/2026dmycj?pageRecorded=true")
PAGE_KEY = "2026dmycj"
ACTIVITY_ID = 17692
TASK_LIST_ID = 55
SIGN_TASK_ID = 3115
DEFAULT_CHANNEL = "P00000011939"
DEFAULT_WTAC = "P00000008211_"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148/wkwebview "
    "leadeon/12.5.2/CMCCIT"
)

# 任务类型：1 浏览/直播；2 分享；3 订购；4/5 领取/关注；6 邀请
SKIP_REASONS = {
    3: "需真实办理/订购",
    6: "需真实好友助力",
}

log = logging.getLogger("cmcc_seed")


class SeedConfig:
    """只取本脚本需要的字段；coc3_phone 缺失时脚本整体跳过。"""

    def __init__(self, path: str):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.path = Path(path)
        self.phone = str(raw.get("phone") or "")
        self.coc3_phone = str(raw.get("coc3_phone") or "")
        self.coc3_channel = str(raw.get("coc3_channel") or DEFAULT_CHANNEL)
        self.coc3_wtac = str(raw.get("coc3_wtac") or DEFAULT_WTAC)
        self.bark_url = raw.get("bark_url", "")
        self.serverchan_sendkey = raw.get("serverchan_sendkey", "")

    @property
    def state_file(self) -> Path:
        return ROOT / f".cmcc_seed_state_{self.phone[-4:]}.json"


class SeedAPI:
    """AI豆苗活动接口；phone 头即凭证，不落任何票据。"""

    def __init__(self, cfg: SeedConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({
            "user-agent": USER_AGENT,
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=utf-8",
            "origin": "https://dev.coc.10086.cn",
            "referer": PAGE_URL,
            "phone": cfg.coc3_phone,
            "accept-language": "zh-CN,zh-Hans;q=0.9",
        })

    def post(self, path: str, body: dict) -> dict:
        """统一 POST；网关异常按失败结构返回，不抛异常。"""
        last = None
        for attempt in (1, 2):
            try:
                r = self.s.post(BASE + path, json=body, timeout=30)
                try:
                    return r.json()
                except ValueError:
                    return {"resultCode": f"HTTP_{r.status_code}", "msg": "响应非 JSON"}
            except requests.RequestException as e:
                last = e
                if attempt == 1:
                    time.sleep(1)
        return {"resultCode": "NETWORK_ERROR", "msg": str(last)}

    def home(self) -> dict:
        return self.post("/api/activity/slgCredits/getSlgCreditsHomePage",
                         {"activityId": ACTIVITY_ID})

    def auth_check(self) -> str:
        """凭证失效探测：返回失效原因，正常返回空串。"""
        r = self.post("/api/activity/slgCredits/queryGrowthValue",
                      {"activityId": ACTIVITY_ID})
        msg = str(r.get("msg") or "")
        if r.get("resultCode") == -516 or "登录" in msg:
            return msg or "登录已失效"
        return ""

    def growth(self) -> int | None:
        r = self.post("/api/activity/slgCredits/queryGrowthValue",
                      {"activityId": ACTIVITY_ID})
        if r.get("resultCode") != 0:
            return None
        return (r.get("data") or {}).get("growthValue")

    def tasks(self) -> dict:
        return self.post("/cmpp-manage/task/getGrowthTaskList",
                         {"taskListId": TASK_LIST_ID})

    def sign_info(self) -> dict:
        return self.post("/cmpp-manage/task/getTaskSignInfo",
                         {"taskId": SIGN_TASK_ID, "needQyCredits": 1, "needLimit": 0})

    def sign(self) -> dict:
        body = {
            "taskId": SIGN_TASK_ID,
            "channelCode": self.cfg.coc3_channel,
            "needLimit": 0,
        }
        if self.cfg.coc3_wtac:
            body["wtAc"] = self.cfg.coc3_wtac
            body["wtAcId"] = str(uuid.uuid4())
        return self.post("/cmpp-manage/task/accumulateSign", body)

    def visit(self, task_id: int, visit_url: str) -> dict:
        return self.post("/cmpp-manage/task/visit", {
            "channelCode": self.cfg.coc3_channel,
            "visitUrl": visit_url,
            "taskId": task_id,
        })

    def share(self, task_id: int) -> dict:
        return self.post("/cmpp-manage/task/share", {
            "channelCode": self.cfg.coc3_channel,
            "visitUrl": PAGE_KEY,
            "taskId": task_id,
        })

    def claim(self, record_id: int) -> dict:
        return self.post("/api/activity/slgCredits/getGrowthBubble", {
            "activityId": ACTIVITY_ID,
            "creditsRecordId": record_id,
            "referer": PAGE_KEY,
            "channelNo": self.cfg.coc3_channel,
        })


def load_state(cfg: SeedConfig) -> dict:
    try:
        return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(cfg: SeedConfig, state: dict):
    try:
        cfg.state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        cfg.state_file.chmod(0o600)
    except Exception:
        pass


def track_token(cfg: SeedConfig, state: dict, persist: bool = True) -> tuple[float, bool]:
    """按 jwt 缓存的 first_seen 方式跟踪 coc3_phone 存活时间。

    只存 token 的 sha256，不落明文；token 变化时重置起点。
    返回 (已存活秒数, 是否为新凭证)。
    """
    now = time.time()
    fp = hashlib.sha256(cfg.coc3_phone.encode("utf-8")).hexdigest()
    if state.get("coc3_phone_hash") != fp:
        state.update(coc3_phone_hash=fp, coc3_phone_first_seen=now,
                     coc3_phone_last_ok=None)
        if persist:
            save_state(cfg, state)
        return 0.0, True
    first = float(state.get("coc3_phone_first_seen") or now)
    return max(0.0, now - first), False


def fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "不到 1 分钟"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def target_key(task: dict) -> str:
    """从任务跳转 URL 取目标页短路径：.../online/yidongaini?x -> yidongaini"""
    url = str(task.get("unfinishRedirectUrl") or "")
    m = re.search(r"/online/([^/?#]+)", url)
    return m.group(1) if m else ""


def run_sign(api: SeedAPI, cfg: SeedConfig, state: dict, dry_run: bool) -> str:
    """每日签到。用本地状态 + accumulateCount 双重判断，避免重复签到。

    accumulateCount 是当前签到周期已签天数；首次运行时若它已大于 0，
    保守认为今天已经签过（可能是在 App 里手动签的），只记录不重复签。
    """
    info = api.sign_info()
    if info.get("resultCode") != 0:
        return f"签到信息获取失败: {info.get('resultCode')} {info.get('msg')}"
    data = info.get("data") or {}
    count = int(data.get("accumulateCount") or 0)
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = state.get("last_sign_date")
    last_count = state.get("last_sign_count")

    if last_date == today:
        return "今日已签到（本地记录），跳过"
    if last_count is not None and count > int(last_count):
        if not dry_run:
            state.update(last_sign_date=today, last_sign_count=count)
            save_state(cfg, state)
        return "今日已签到（签到天数已增加），跳过"
    if last_count is None and count > 0:
        if not dry_run:
            state.update(last_sign_date=today, last_sign_count=count)
            save_state(cfg, state)
        return "检测到已有签到记录，今日不重复签到"
    if dry_run:
        return f"签到状态：累计 {count} 天，今日待签到"

    resp = api.sign()
    awards = resp.get("data") or []
    if resp.get("resultCode") == 0 and isinstance(awards, list) and awards:
        names = "、".join(str(a.get("awardName") or "奖励") for a in awards)
        state.update(last_sign_date=today, last_sign_count=count + 1)
        save_state(cfg, state)
        return f"签到成功：{names}"
    msg = str(resp.get("msg") or "")
    code = resp.get("resultCode")
    if code in (500, 502) or "已签" in msg or "重复" in msg:
        state.update(last_sign_date=today, last_sign_count=count)
        save_state(cfg, state)
        return f"今日已签到（{msg or code}），跳过"
    return f"签到失败: {code} {msg}"


def _do_task(api: SeedAPI, task: dict) -> tuple[dict, str]:
    """执行一个浏览/分享任务，返回 (响应, 使用的动作)。"""
    ttype = task.get("taskType")
    tid = task.get("taskId")
    if ttype == 2:
        return api.share(tid), "share"
    return api.visit(tid, target_key(task)), "visit"


def run_tasks(api: SeedAPI, dry_run: bool) -> tuple[list[str], int]:
    """按任务清单跑可代做任务；返回 (结果行, 完成数)。"""
    r = api.tasks()
    if r.get("resultCode") != 0:
        return [f"任务清单获取失败: {r.get('resultCode')} {r.get('msg')}"], 0
    tasks = r.get("data") or []
    todo = [t for t in tasks if t.get("isFinish") == 0]
    lines: list[str] = []

    if dry_run:
        for t in todo:
            title = t.get("taskMainTitle") or f"任务{t.get('taskId')}"
            ttype = t.get("taskType")
            if ttype in SKIP_REASONS:
                lines.append(f"· {title}（{SKIP_REASONS[ttype]}，跳过）")
            elif ttype == 2:
                lines.append(f"→ {title}（分享，可自动完成）")
            elif ttype == 1 and target_key(t):
                lines.append(f"→ {title}（浏览/直播，可自动完成）")
            elif target_key(t):
                lines.append(f"→ {title}（领取/关注类，将尝试）")
            else:
                lines.append(f"· {title}（缺少目标页，跳过）")
        if not todo:
            lines.append("任务已全部完成")
        return lines, 0

    done = 0
    accepted: set[int] = set()
    for t in todo:
        title = t.get("taskMainTitle") or f"任务{t.get('taskId')}"
        ttype = t.get("taskType")
        tid = t.get("taskId")
        if ttype in SKIP_REASONS:
            lines.append(f"· {title}（{SKIP_REASONS[ttype]}，跳过）")
            continue
        if ttype not in (1, 2, 4, 5):
            lines.append(f"· {title}（类型 {ttype}，跳过）")
            continue
        if ttype != 2 and not target_key(t):
            lines.append(f"· {title}（缺少目标页，跳过）")
            continue

        resp, _action = _do_task(api, t)
        data = resp.get("data")
        if isinstance(data, str) and "频繁" in data:
            time.sleep(5)
            resp, _action = _do_task(api, t)
            data = resp.get("data")

        if resp.get("resultCode") != 0:
            lines.append(f"✗ {title}：{resp.get('msg') or resp.get('resultCode')}")
        elif data is None or data is True:
            # 上报已接受；不再逐个等 1.5 秒复查，最后统一查一次
            lines.append(f"✓ {title} 已上报")
            accepted.add(tid)
            done += 1
        elif isinstance(data, str):
            lines.append(f"· {title}：{data}")
        else:
            lines.append(f"· {title}：{data}")
        time.sleep(random.uniform(0.8, 1.5))

    # 统一复查：只检查已接受上报的任务，状态未刷新时提示（奖励可能仍在路上）
    if accepted:
        time.sleep(2)
        chk = api.tasks()
        if chk.get("resultCode") == 0:
            still = [t.get("taskMainTitle") or t.get("taskId")
                     for t in chk.get("data") or []
                     if t.get("taskId") in accepted and t.get("isFinish") != 1]
            if still:
                lines.append(f"· 复查：{len(still)} 个任务状态待刷新")
    if not lines:
        lines.append("任务已全部完成")
    return lines, done


def claim_all(api: SeedAPI, dry_run: bool) -> tuple[list[str], int]:
    """领取所有待领阳光值；返回 (结果行, 阳光值合计)。"""
    r = api.home()
    if r.get("resultCode") != 0:
        return [f"待领阳光值获取失败: {r.get('resultCode')} {r.get('msg')}"], 0
    records = (r.get("data") or {}).get("unreceivedCreditsVOList") or []
    if not records:
        return ["待领阳光值：无"], 0
    total = sum(int(x.get("showNum") or 0) for x in records)
    if dry_run:
        names = "、".join(f"{x.get('showName')}+{x.get('showNum')}" for x in records)
        return [f"待领阳光值 {len(records)} 条（共 {total}）：{names}"], 0

    lines = []
    got = 0
    for rec in records:
        resp = api.claim(rec.get("recordId"))
        name = rec.get("showName") or "阳光值"
        num = int(rec.get("showNum") or 0)
        if resp.get("resultCode") == 0:
            lines.append(f"✓ 领取 {name} +{num}")
            got += num
        else:
            lines.append(f"✗ 领取 {name} 失败: {resp.get('msg') or resp.get('resultCode')}")
        time.sleep(0.5)
    return lines, got


def run(config_path: str, dry_run: bool = False, no_sign: bool = False,
        no_claim: bool = False, notify_result: bool = True) -> tuple[int, str]:
    """执行一次 AI豆苗活动；返回 (退出码, 结果摘要)。

    notify_result=False 时由调用方（cmcc_sign.py）统一推送，避免重复通知。
    """
    cfg = SeedConfig(config_path)
    if not cfg.coc3_phone:
        msg = "未配置 coc3_phone，跳过 AI豆苗养成记"
        log.info(msg)
        return 0, msg

    state = load_state(cfg)
    age_sec, new_token = track_token(cfg, state, persist=not dry_run)
    api = SeedAPI(cfg)
    auth_err = api.auth_check()
    if auth_err:
        last_ok = state.get("coc3_phone_last_ok")
        extra = (f"，上次成功 {fmt_age(time.time() - float(last_ok))} 前"
                 if last_ok else "")
        msg = (f"coc3_phone 已失效（{auth_err}），已存活 {fmt_age(age_sec)}"
               f"{extra}，请重新抓包更新")
        log.warning(msg)
        if notify_result:
            notify(cfg, "AI豆苗养成记", f"{msg}\n手机尾号 {cfg.phone[-4:]}")
        return 1, msg
    if not dry_run:
        state["coc3_phone_last_ok"] = time.time()
        save_state(cfg, state)
    token_line = (f"coc3_phone 已存活 {fmt_age(age_sec)}"
                  + ("（新凭证，从本次开始计时）" if new_token else ""))
    log.info("[凭证] %s", token_line)
    growth_before = api.growth()

    sign_line = "跳过签到" if no_sign else run_sign(api, cfg, state, dry_run)
    task_lines, done = run_tasks(api, dry_run)
    claim_lines, got = (["跳过领取"] if no_claim
                        else claim_all(api, dry_run))
    growth_after = api.growth()
    # 入账有延迟时不再等待：任务/领取已经完成，余额以活动页稍后刷新为准
    lagged = (not dry_run and got and growth_before is not None
              and growth_after is not None
              and growth_after < growth_before + got)

    summary = "\n".join([
        f"[凭证] {token_line}",
        f"[签到] {sign_line}",
        *task_lines,
        *claim_lines,
        f"[阳光值] {growth_before if growth_before is not None else '?'}"
        f" → {growth_after if growth_after is not None else '?'}"
        + (f"（本次领取 +{got}）" if got else "")
        + ("（入账延迟，请以活动页余额为准）" if lagged else ""),
    ])
    log.info("\n%s", summary)
    if notify_result and not dry_run:
        notify(cfg, "AI豆苗养成记",
               f"{summary}\n手机尾号 {cfg.phone[-4:]}")
    return 0, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="AI豆苗养成记自动任务")
    ap.add_argument("--config", default=str(ROOT / "config.json"),
                    help="默认根目录 config.json")
    ap.add_argument("--dry-run", action="store_true", help="只查状态，不做写操作")
    ap.add_argument("--no-sign", action="store_true", help="跳过签到")
    ap.add_argument("--no-claim", action="store_true", help="跳过领取阳光值")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    rc, _ = run(args.config, dry_run=args.dry_run, no_sign=args.no_sign,
                no_claim=args.no_claim)
    return rc


if __name__ == "__main__":
    sys.exit(main())
