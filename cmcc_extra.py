#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动 qwhdhub 拓展活动模块（cmcc_sign.py 的可选扩展）
=========================================================

在 mark31 主签到之外，覆盖同一 SSO 通道下的另外三类收益：

  A. 代币任务（diyTask 体系）
     GET  /qwhdhub/diyTask/list/<componentId>     任务清单（taskStage=UNDO/DONE）
     POST /qwhdhub/diyTask/finish/<taskId>        完成任务并领取代币
     browse / inapp_cdt / share 等类型任务均可通过该端点完成上报并领取代币。

  B. 签到页 AI豆任务（mark/task 体系，与 diyTask 是两套独立端点）
     POST /qwhdhub/api/mark/task/taskList         任务清单（status=0 为待办）
     POST /qwhdhub/api/mark/task/finishTask       {taskId, taskType}
     POST /qwhdhub/api/mark/task/getTaskAward     领取
     请求需携带目标页到访标识与 Referer（该任务 jumpUrl）及停留时长 scanTime；
     sign/random 等前端计算字段可省略。
     跳转类任务（响应提示"特殊处理"）走 hlwyxhdhub 握手，见 open_finish_task()。

  C. 抽奖消耗（diyLottery 体系）
     POST /qwhdhub/diyLottery/period/remain/<componentId>   余额与剩余次数（只读预检）
     GET  /qwhdhub/diyLottery/lotterySafely/<componentId>   执行一次抽奖，无 body

  D. 追剧领福利「看精彩视频」每日任务（qwhdhub + cmlive 双域名）
     GET  /qwhdhub/api/diyVideoDayRedesign/task/queryDiyVideoTask  任务/jtToken
     GET  /qwhdhub/user/getYDRZToken?targetSourceId=017027        换视频站票据
     POST /cmlive/api/user/auth/cmpassLoginByCmToken              换视频站 JWT
     GET  /cmlive/api/advert/lottery/getFiveMinuteTaskInfo        下发 cToken
     GET  /cmlive/api/advert/lottery/fiveMinuteTaskFinish         完成（+1 抽奖机会）
     前端有 5 分钟倒计时；实测拿到 cToken 后可直接完成上报。

会话与凭证完全复用 cmcc_sign.exchange_session()（jwt 自续期缓存同样生效），
本模块只负责业务接口，不落任何凭证。

用法（由 cmcc_sign.py 转发）：
  python3 cmcc_sign.py --tasks     # 主签到 + 领 AI豆任务
  python3 cmcc_sign.py --games     # 三活动签到 + 代币任务 + 每日看视频 + 到窗口自动抽奖
  python3 cmcc_sign.py --games --dry-run   # 只报状态与余额，不做任何写操作
"""

import logging
import random
import re
import time
from datetime import datetime

import requests

from cmcc_sign import (
    BASE,
    LegacyTLSAdapter,
    USER_AGENT,
    api_headers,
    assert_safe_url,
    exchange_session,
)

log = logging.getLogger("cmcc_sign")

# 视频站（cmlive）与 qwhdhub 是两套会话：QWHD 会话只能经 getYDRZToken
# 换 YDRZ 票据，再调 cmpassLoginByCmToken 换视频站 JWT。017027 是 App
# 原生 getYDRZToken 使用的 sourceId（抓包实测，001191 会被视频站拒绝）。
CMLIVE_BASE = "https://v.cmcc-cs.cn/cmlive/api"
CMLIVE_SOURCE_ID = "017027"
# 仅作 Referer/播放页标识；jtToken 由任务下发
VIDEO_REFERER = ("https://v.cmcc-cs.cn/cmlive/live/feed?id=102518"
                 "&cmliveChannelId=203&belongChannelId=203&secondChannel=spr"
                 "&openSelfResearch=1&tagId=157&jtCode=1")

# 活动页 URL 为公开配置（非个人凭证）；若你的省份/渠道入口不同，替换 query 参数即可
ACTS = {
    "saturday": {
        "name": "周六游戏中心",
        "entry": f"{BASE}/qwhdhub/diy-client/1126086939?A_C_CODE=pXMkQhEmZ7&channelId=P00000119581",
        "sign": ("POST", "/qwhdhub/api/diySaturdayGame/clockIn", {},
                 "POST", "/qwhdhub/api/diySaturdayGame/getClockInStatus"),
        "signed": lambda d: bool(d.get("signed")) or "已签到" in (d.get("message") or ""),
        "task_component": "50HQAg0-9YaSW4St7MDT",
        # 游戏币 10 币/次，全时段可抽；周六 12:00 起奖池拓展（非周六只攒不抽），
        # 余额按 31 天周期清零，务必在清零前抽光；未中即停，防奖池抽空后白扣币
        "lottery": {"component": "50HQAg0-9YaSW4St7MDT", "wallet_key": "remainGameVal",
                    "cost": 10, "count_key": "remain", "weekday": 6, "start_hour": 12,
                    "label": "游戏币", "stop_on_miss": True},
    },
    "video": {
        "name": "追剧领福利",
        "entry": f"{BASE}/qwhdhub/diy-client/1126082530?A_C_CODE=10hep1ZhkL&channelId=P00000119581",
        "sign": ("GET", "/qwhdhub/api/diyVideoDayRedesign/sign/doSign", None,
                 "GET", "/qwhdhub/api/diyVideoDayRedesign/sign/querySignStatus"),
        "signed": lambda d: d.get("todaySignFlag") == "1",
        "task_component": "4EC96l0-9btuobajC9Yu",
        # 每日「看精彩视频」任务：+1 抽奖机会。
        # 视频页在 cmlive 域，鉴权/上报接口与 qwhdhub 不同，单独处理。
        "video_day": True,
        # 抽奖次数 1 次/抽；服务端 consumeRestrictionType=1，仅周日开放，
        # 次数过期作废 → 未中奖也抽满（stop_on_miss=False）
        "lottery": {"component": "4EC96l0-9btuobajC9Yu", "wallet_key": "remain",
                    "cost": 1, "count_key": None, "weekday": 7,
                    "label": "次数", "stop_on_miss": False},
    },
    "game666": {
        "name": "玩游戏抽话费（周任务）",
        "entry": f"{BASE}/qwhdhub/diy-client/1126090514?A_C_CODE=aEvxYYAV3C&channelId=P00000119581",
        "sign": None,  # 该页无每日打卡，只有每周一刷新的任务
        "task_component": "7M6vIq0-TFMj5T-ZGjks",
    },
}

# 跳转任务握手页（hlwyxhdhub 域，会话令牌名为 HLWHD_SESSION_TOKEN）
OPEN_TASK_ENTRY = (f"{BASE}/hlwyxhdhub/act-wedrecharge/1024101716"
                   "?pageId=1849008675699650560&channelId=P00000093619")


class ActConfig:
    """复用主脚本 Config 的凭证字段，仅把 act_url 换成指定活动页。

    exchange_session 按 Referer 路由签发会话，不同活动页需要各自换一次票；
    jwt 是账号级凭证，跨活动/跨 hub 通用，因此换票不会消耗新的 app_token。
    """

    def __init__(self, base_cfg, entry: str):
        self.__dict__.update(vars(base_cfg))
        self._entry = entry

    @property
    def act_url(self) -> str:
        return self._entry


def api_call(s: requests.Session, referer: str, method: str, path: str, body=None) -> dict:
    """统一 API 调用；网关偶发返回 HTML（限流/会话过期），按失败结构返回而非抛异常。"""
    headers = api_headers(referer)
    try:
        if method == "GET":
            r = s.get(BASE + path, headers=headers, timeout=30)
        else:
            r = s.post(BASE + path, json=body or {}, headers=headers, timeout=30)
        return r.json()
    except ValueError:
        return {"code": f"HTTP_{getattr(r, 'status_code', 0)}", "msg": "响应非 JSON"}
    except requests.RequestException as e:
        return {"code": "NETWORK_ERROR", "msg": str(e)}


def run_diy_tasks(s: requests.Session, referer: str, component: str, dry_run: bool) -> str:
    """代币任务：对每个 UNDO 任务 POST finish 即发币。"""
    resp = api_call(s, referer, "GET", f"/qwhdhub/diyTask/list/{component}")
    tasks = resp.get("data") or []
    if not tasks and resp.get("code") != "SUCCESS":
        return f"任务清单获取失败: {resp.get('code')} {resp.get('msg')}"
    todo = [t for t in tasks if t.get("taskStage") == "UNDO"]
    if dry_run or not todo:
        return f"代币任务 {len(tasks)} 个，待办 {len(todo)} 个"

    done = 0
    for t in todo:
        tid = t.get("taskId")
        if not tid:
            continue
        api_call(s, referer, "POST", f"/qwhdhub/diyTask/finish/{tid}", {})
        # 复查清单：完成后任务可能直接移出列表，按"消失"也算成功
        chk = api_call(s, referer, "GET", f"/qwhdhub/diyTask/list/{component}")
        cur = next((x for x in (chk.get("data") or []) if x.get("taskId") == tid), None)
        if cur is None or cur.get("taskStage") == "DONE":
            done += 1
        time.sleep(random.uniform(1, 2.5))
    return f"代币任务 {len(todo)} 个待办，完成 {done} 个"


def run_lottery(s: requests.Session, referer: str, cfg: dict, dry_run: bool) -> str:
    """抽奖消耗：只读预检余额 → 到窗口才真扣。

    幂等由服务端余额保证（余额为 0 时无事可做），重复运行不会多扣资产。
    非窗口日只报余额不消耗：游戏币攒到周六拓展池、追剧次数仅周日开放。
    NOT_WON 是正常「未中奖」结果（次数/币照扣），不是接口错误：
    stop_on_miss=True 未中即停（防奖池抽空后白扣币），False 抽满为止（次数过期作废）。
    """
    comp = cfg["component"]
    rem = api_call(s, referer, "POST", f"/qwhdhub/diyLottery/period/remain/{comp}", {})
    if rem.get("code") != "SUCCESS":
        return f"抽奖[{cfg['label']}] 余额查询失败: {rem.get('code')} {rem.get('msg')}"
    d = rem.get("data") or {}
    wallet = d.get(cfg["wallet_key"]) or 0
    limit = d.get(cfg["count_key"]) if cfg.get("count_key") else None
    n = wallet // cfg["cost"]
    if limit is not None:
        n = min(n, int(limit))
    head = f"抽奖[{cfg['label']}] 余额 {wallet}（{cfg['cost']}/次）→ 可抽 {n} 次"
    if dry_run or n <= 0:
        return head

    now = datetime.now()
    if (cfg.get("weekday") and now.isoweekday() != cfg["weekday"]) or \
       (cfg.get("start_hour") and now.hour < cfg["start_hour"]):
        window = f"周{cfg['weekday']}" + (f" {cfg['start_hour']}:00 起" if cfg.get("start_hour") else "")
        return f"{head}；未到窗口（{window}），本次不消耗"

    results = []
    stop_on_miss = cfg.get("stop_on_miss", True)
    for _ in range(n):
        r = api_call(s, referer, "GET", f"/qwhdhub/diyLottery/lotterySafely/{comp}")
        won = (r.get("data") or [{}])[0] if r.get("code") == "SUCCESS" else None
        if won:
            results.append(str(won.get("prizeName") or "?"))
        elif r.get("code") in ("SUCCESS", "NOT_WON"):
            results.append("未中奖" + ("(即停)" if stop_on_miss else ""))
            if stop_on_miss:
                break
        else:
            results.append(f"失败({r.get('code')} {r.get('msg')})")
            break
        time.sleep(random.uniform(1.2, 2.8))
    return f"{head}，已抽 {len(results)} 次: {' / '.join(results)}"


def open_task_session(cfg: ActConfig, cache: dict):
    """开放平台握手用的 hlwyxhdhub 会话，跨任务复用（cache 为 {"s":..,"ref":..}）。

    SSO 换票偶发失败（当日多次换票后限频），重试一次。
    """
    if cache.get("s"):
        return cache["s"], cache["ref"]
    last_err = None
    for wait in (0, 3):
        if wait:
            time.sleep(wait)
        try:
            s2, ref2 = exchange_session(ActConfig(cfg, OPEN_TASK_ENTRY))
            cache.update(s=s2, ref=ref2)
            return s2, ref2
        except RuntimeError as e:
            last_err = e
    raise last_err


def open_handshake(cfg: ActConfig, cache: dict, jt_token: str, scan: int = 0) -> bool:
    """开放平台任务握手：getOneTaskInfo 取 cToken → openFinish。

    先立即上报（实测可直接完成），失败再取新 cToken 并按客户端流程
    等满 scanTime 重试，兼顾速度与成功率。
    """
    try:
        s2, ref2 = open_task_session(cfg, cache)

        def get_ct() -> tuple[str, dict]:
            info = api_call(s2, ref2, "POST",
                            "/hlwyxhdhub/api/open/_pub/task/getOneTaskInfo",
                            {"jtToken": jt_token})
            d = info.get("data") or {}
            return d.get("cToken") or "", d

        ct, d = get_ct()
        if not ct:
            return False
        fin = api_call(s2, ref2, "POST",
                       "/hlwyxhdhub/api/open/_pub/task/openFinish", {"cToken": ct})
        if fin.get("code") == "SUCCESS":
            return True
        # 回退：服务端校验停留时长时，换新 cToken 等满 scanTime 再上报
        ct, d = get_ct()
        if not ct:
            return False
        time.sleep(int(d.get("scanTime") or scan or 0) + 1)
        fin = api_call(s2, ref2, "POST",
                       "/hlwyxhdhub/api/open/_pub/task/openFinish", {"cToken": ct})
        return fin.get("code") == "SUCCESS"
    except Exception as e:
        log.debug("开放平台握手失败: %s", e)
        return False


def open_finish_task(cfg: ActConfig, task: dict, cache: dict | None = None) -> bool:
    """跳转类任务握手：jumpUrl 自带 taskToken → 握手完成。
    成功后由调用方回主站 getTaskAward 领奖。

    cache 传入 {"s":..,"ref":..} 字典时跨任务复用同一 hlwyxhdhub 会话，
    避免每个任务各做一次完整 SSO 换票。
    """
    ju = str(task.get("jumpUrl") or "")
    m = re.search(r"taskToken=([^&]+)", ju)
    if not m:
        return False
    return open_handshake(cfg, cache or {}, m.group(1))


def run_mark_tasks(s: requests.Session, referer: str, cfg, dry_run: bool) -> str:
    """签到页 AI豆任务（mark/task 体系）。

    按任务类别自适应：
    - taskInfo 下发 taskToken 的任务（视频/浏览类）：开放平台握手即完成
      （getOneTaskInfo → 先立即 openFinish，失败再等 scanTime 重试），
      无需 finishTask；
    - 其余任务：finishTask 阶梯——直接上报 → 失败则访问目标页后立即换
      目标页 Referer 重试（仍失败再等 scanTime）→ 仍失败且提示特殊处理
      再走握手。
    握手会话跨任务复用，避免逐任务完整换票；taskType/scanTime 取自 taskInfo
    （taskList 的 taskType 是另一套枚举，不可混用）。
    """
    begun = time.monotonic()
    tl = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/taskList", {})
    tasks = (tl.get("data") or {}).get("tasks") or []
    # 外部合作区任务由合作方服务端回调记账，客户端无法代做，整区跳过
    ext = [t for t in tasks if t.get("status") == 0
           and t.get("taskClasifiName") == "外部合作区"]
    todo = [t for t in tasks if t.get("status") == 0
            and t.get("taskClasifiName") != "外部合作区"]
    if dry_run or not todo:
        return (f"AI豆任务 {len(tasks)} 个，待办 {len(todo)} 个"
                f"（另有外部合作区 {len(ext)} 个不做）")

    open_cache: dict = {}
    got = skipped = 0

    # 上次运行握手中断/未领取的遗留（status=1 已完成待领奖）先补领
    for t in [x for x in tasks if x.get("status") == 1]:
        tid = str(t.get("taskId") or "")
        aw = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/getTaskAward",
                      {"taskId": tid})
        if aw.get("code") == "SUCCESS":
            n = (aw.get("data") or {}).get("awardNum")
            log.info("[AI豆] 补领 %s +%s", t.get("taskName", tid), n)
            got += 1
        time.sleep(random.uniform(0.2, 0.5))

    def finish(tid, ttype, hdr_referer=None):
        h = api_headers(hdr_referer) if hdr_referer else api_headers(referer)
        return s.post(BASE + "/qwhdhub/api/mark/task/finishTask",
                      json={"taskId": tid, "taskType": ttype}, headers=h, timeout=30).json()

    for t in todo:
        tid = str(t.get("taskId") or "")
        if not tid:
            continue
        ju = str(t.get("jumpUrl") or "")
        info = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/taskInfo", {"taskId": tid})
        d = info.get("data") or {}
        task_type = str(d.get("taskType") or "")
        scan = int(d.get("scanTime") or 0)
        task_token = d.get("taskToken") or ""

        done = False
        if task_token:
            # 开放平台任务：握手即完成
            done = open_handshake(cfg, open_cache, task_token, scan)
        if not done:
            r = finish(tid, task_type)
            msg = r.get("msg") or ""
            if r.get("code") != "SUCCESS" and "公众号" in msg:
                # 需真实关注公众号，无法代做，立即放弃不重试
                skipped += 1
                time.sleep(random.uniform(0.2, 0.5))
                continue
            # 访问目标页后带目标页 Referer 重试：先立即试，失败再停留
            # scanTime 重试（服务端若已登记到访，立即重试即可过）
            if r.get("code") != "SUCCESS" and ju.startswith("https://"):
                try:
                    assert_safe_url(ju)
                    s.get(ju, timeout=15)
                    r = finish(tid, task_type, ju)
                    if r.get("code") != "SUCCESS" and scan:
                        time.sleep(min(scan, 15) + 1)
                        r = finish(tid, task_type, ju)
                except (ValueError, requests.RequestException):
                    pass
                msg = r.get("msg") or ""
            # 提示特殊处理：jumpUrl 自带 taskToken 的走握手
            if r.get("code") != "SUCCESS" and ("特殊处理" in msg or "openFinish" in msg):
                done = open_finish_task(cfg, t, open_cache)
                if done:
                    r = {"code": "SUCCESS"}

        if done or r.get("code") == "SUCCESS":
            aw = api_call(s, referer, "POST", "/qwhdhub/api/mark/task/getTaskAward",
                          {"taskId": tid})
            n = (aw.get("data") or {}).get("awardNum") if aw.get("code") == "SUCCESS" else "?"
            log.info("[AI豆] %s +%s", t.get("taskName", tid), n)
            got += 1
        else:
            skipped += 1
        time.sleep(random.uniform(0.2, 0.6))
    cost = time.monotonic() - begun
    return (f"AI豆任务 {len(todo)} 个待办，完成 {got} 个，跳过 {skipped} 个"
            f"（第三方 App/真实操作类），外部合作区 {len(ext)} 个未计入，耗时 {cost:.0f}s")


def cmlive_headers(referer: str, jwt: str = "") -> dict:
    """视频站 API 头；jwt 为 cmpassLoginByCmToken 换到的 30 天 JWT。"""
    h = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://v.cmcc-cs.cn",
        "referer": referer,
        "x-requested-with": "XMLHttpRequest",
    }
    if jwt:
        h["authorization"] = jwt
    return h


def open_cmlive_session(s: requests.Session, referer: str) -> tuple[requests.Session, str] | None:
    """QWHD 会话 → YDRZ 票据 → 视频站 JWT（全自动，无需抓包凭证）。

    视频页在 App 内用原生 getYDRZToken(sourceId=017027) 换票，这里复用
    qwhdhub 的同名 HTTP 接口取票，再调 cmpassLoginByCmToken 换 JWT。
    """
    try:
        r = s.get(f"{BASE}/qwhdhub/user/getYDRZToken",
                  params={"targetSourceId": CMLIVE_SOURCE_ID},
                  headers=api_headers(referer), timeout=30)
        ydrz = r.json().get("data") or ""
    except (ValueError, requests.RequestException) as e:
        log.warning("[视频] YDRZ 取票失败: %s", e)
        return None
    if not ydrz:
        log.warning("[视频] YDRZ 取票为空（sourceId=%s）", CMLIVE_SOURCE_ID)
        return None

    # 视频站只接受老式 TLS 套件，且必须直连（避免本机代理 MITM 证书）
    cs = requests.Session()
    cs.trust_env = False
    cs.mount("https://", LegacyTLSAdapter())
    cs.headers.update({"user-agent": USER_AGENT,
                       "accept-language": "zh-CN,zh-Hans;q=0.9"})
    try:
        r = cs.post(f"{CMLIVE_BASE}/user/auth/cmpassLoginByCmToken",
                    json={"cmpassToken": ydrz},
                    headers=cmlive_headers(VIDEO_REFERER, ""), timeout=30)
        resp = r.json()
    except (ValueError, requests.RequestException) as e:
        log.warning("[视频] 视频站登录失败: %s", e)
        return None
    jwt = resp.get("object") if isinstance(resp.get("object"), str) else ""
    if resp.get("rtnCode") != "0" or not jwt:
        log.warning("[视频] 视频站登录被拒: %s %s", resp.get("rtnCode"), resp.get("rtnMsg"))
        return None
    return cs, jwt


def cmlive_get(cs: requests.Session, jwt: str, path: str, params: dict,
               referer: str) -> dict:
    """视频站 GET；网关异常按失败结构返回，不抛异常。"""
    try:
        r = cs.get(f"{CMLIVE_BASE}{path}", params=params,
                   headers=cmlive_headers(referer, jwt), timeout=30)
        return r.json()
    except ValueError:
        return {"rtnCode": f"HTTP_{getattr(r, 'status_code', 0)}", "rtnMsg": "响应非 JSON"}
    except requests.RequestException as e:
        return {"rtnCode": "NETWORK_ERROR", "rtnMsg": str(e)}


def video_remain(s: requests.Session, referer: str) -> int | None:
    """视频日抽奖次数余额（只读），用于核对任务是否真的 +1。"""
    r = api_call(s, referer, "POST",
                 "/qwhdhub/diyLottery/period/remain/4EC96l0-9btuobajC9Yu", {})
    if r.get("code") != "SUCCESS":
        return None
    return (r.get("data") or {}).get("remain")


def run_video_day(s: requests.Session, referer: str, dry_run: bool) -> str:
    """视频日「看精彩视频」每日任务：+1 抽奖机会。

    链路（抓包 + 前端 JS 双重确认）：
      queryDiyVideoTask 取任务/jtToken
      → startBrowse 登记「去完成」（不跟随跳转）
      → getFiveMinuteTaskInfo(token=jtToken) 下发 cToken
      → fiveMinuteTaskFinish(token=cToken) 完成任务

    前端有 300 秒倒计时；实测拿到 cToken 后可直接上报成功，因此这里不等待。

    任务名随星期变化（如「视频非周日六」「视频周日」），按 taskStage=UNDO
    且 todayRemain>0 自适应选取，不写死任务 ID。
    """
    r = api_call(s, referer, "GET", "/qwhdhub/api/diyVideoDayRedesign/task/queryDiyVideoTask")
    data = r.get("data") or {}
    if r.get("code") != "SUCCESS":
        return f"视频任务清单获取失败: {r.get('code')} {r.get('msg')}"
    todo = [t for t in data.get("undoTodayTaskList") or []
            if t.get("taskStage") == "UNDO"
            and int(t.get("todayRemain") or 0) > 0
            and int(t.get("earnOpportunity") or 0) > 0]
    if not todo:
        return "视频任务今日已完成（或已到上限）"
    # 任务名随星期变化（视频非周日六/视频周日…），按“视频”或 cmlive
    # 目标页识别；只有单个待办时直接用它，避免服务端改名后漏做
    video_todo = [t for t in todo
                  if "视频" in f"{t.get('name') or ''}{t.get('description') or ''}"
                  or "cmlive" in str(t.get("url") or "")]
    if not video_todo and len(todo) == 1:
        video_todo = todo
    if not video_todo:
        return "今日待办中没有可识别的视频任务"
    task = video_todo[0]
    name = task.get("name") or "看精彩视频"
    if dry_run:
        return f"视频任务待办：{name}（正式运行自动完成）"

    jt = str(task.get("param") or "")
    if not jt.startswith("QWHD_") and task.get("taskId"):
        jt = "QWHD_" + str(task["taskId"])
    if not jt:
        return f"视频任务 {name} 缺少 jtToken"

    # ① 与 App 内点「去完成」一致：登记浏览入口。失败不阻断，
    #    真正的完成判定在 fiveMinuteTaskFinish。
    turl = str(task.get("url") or "")
    if turl:
        try:
            target = BASE + turl if turl.startswith("/") else turl
            if target.startswith(BASE):
                s.get(target, headers=api_headers(referer), timeout=30,
                      allow_redirects=False)
            else:
                assert_safe_url(target, allowed_hosts={"wx.10086.cn"})
                s.get(target, headers=api_headers(referer), timeout=30,
                      allow_redirects=False)
        except (ValueError, requests.RequestException) as e:
            log.debug("[视频] startBrowse 失败（不阻断）: %s", e)

    # ② 视频站会话 + cToken
    opened = open_cmlive_session(s, referer)
    if not opened:
        return f"视频任务 {name} 失败：视频站登录未成功"
    cs, jwt = opened

    ref = VIDEO_REFERER + "&jtToken=" + jt
    remain_before = video_remain(s, referer)
    info = cmlive_get(cs, jwt, "/advert/lottery/getFiveMinuteTaskInfo",
                      {"token": jt}, ref)
    obj = info.get("object") or {}
    d = obj.get("data") or {}
    if info.get("rtnCode") != "0" or obj.get("code") != "SUCCESS" or not d:
        return (f"视频任务 {name} 领取失败: "
                f"{info.get('rtnCode')} {obj.get('msg') or info.get('rtnMsg')}")
    status = str(d.get("status") or "")
    if status == "0":
        return f"视频任务今日已完成（{d.get('finishDesc') or '奖励已发放'}）"
    if status != "1":
        return f"视频任务状态异常（status={status or '空'}），暂不观看"
    ctoken = d.get("cToken") or ""
    if not ctoken:
        return f"视频任务 {name} 未下发 cToken"

    # ③ 直接完成上报（实测无需等待倒计时）
    fin = cmlive_get(cs, jwt, "/advert/lottery/fiveMinuteTaskFinish",
                     {"token": ctoken}, ref)
    fobj = fin.get("object") or {}
    if fin.get("rtnCode") != "0" or not fobj.get("data"):
        return (f"视频任务 {name} 完成上报失败: "
                f"{fin.get('rtnCode')} {fobj.get('msg') or fin.get('rtnMsg')}")

    # ⑤ 复查任务与次数
    chk = api_call(s, referer, "GET",
                   "/qwhdhub/api/diyVideoDayRedesign/task/queryDiyVideoTask")
    cdata = chk.get("data") or {}
    done = (int(cdata.get("doneDayCount") or 0) > int(data.get("doneDayCount") or 0)
            or not any(t.get("taskId") == task.get("taskId")
                       for t in cdata.get("undoTodayTaskList") or []))
    remain_after = video_remain(s, referer)
    add = (f"（+{remain_after - remain_before} 次）"
           if remain_before is not None and remain_after is not None
           and remain_after > remain_before else "")
    if done:
        return f"视频任务 {name} 完成{add}"
    return f"视频任务 {name} 已上报，但状态未刷新，请稍后复查"


def run_games(cfg, dry_run: bool) -> list[str]:
    """跑三个拓展活动：换票 → 打卡 → 代币任务 → 抽奖。返回结果行。"""
    lines = []
    for key, act in ACTS.items():
        s, referer = exchange_session(ActConfig(cfg, act["entry"]))
        log.info("[%s] 会话建立成功", act["name"])

        if act["sign"]:
            do_method, do_path, do_body, q_method, q_path = act["sign"]
            st = api_call(s, referer, q_method, q_path, {} if q_method == "POST" else None)
            data = st.get("data") or {}
            if act["signed"](data):
                lines.append(f"{act['name']}: 今日已打卡")
            elif dry_run:
                lines.append(f"{act['name']}: [dry-run] 今日未打卡")
            else:
                r = api_call(s, referer, do_method, do_path, do_body)
                ok = r.get("code") == "SUCCESS" or "已签" in (r.get("msg") or "")
                lines.append(f"{act['name']}: 打卡{'成功' if ok else '失败 ' + str(r.get('msg'))}")

        if act.get("task_component"):
            lines.append(f"{act['name']} {run_diy_tasks(s, referer, act['task_component'], dry_run)}")
        if act.get("video_day"):
            # 放在抽奖之前：视频任务 +1 次，周日抽奖能立刻用上
            lines.append(f"{act['name']} {run_video_day(s, referer, dry_run)}")
        if act.get("lottery"):
            lines.append(f"{act['name']} {run_lottery(s, referer, act['lottery'], dry_run)}")
        time.sleep(random.uniform(1.5, 4))
    return lines
