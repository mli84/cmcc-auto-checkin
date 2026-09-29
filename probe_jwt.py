#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jwt 寿命探测实验（冻结样本老化法）
====================================

原理：把一枚 jwt 冻结为样本（不再参与日常签到），定期用它尝试续期。
它失败的那一天减去签发时刻，就是 jwt 的实际存活时长（±探测间隔精度）。

与日常使用的凭证链（cmcc_sign.py 自动维护）对照，可以区分两种假设：
  - 冻结样本与日常链同日死亡 → 服务端按「签发时刻」计时，寿命固定；
  - 冻结样本死亡而日常链持续存活 → 服务端按「最近使用」计时，
    每次成功续期都在刷新时钟（对自动化最有利的情形）；
  - 冻结样本长期不死 → 寿命极长或探测本身在续命。

样本文件 jwt_probe_samples.json（含凭证，已被 .gitignore 排除）：
  [{"label": "2026-09-29 14:50 抓包", "issued_at": 1790686231, "jwt": "..."}]

用法：手动执行，或挂系统 crontab（建议每周一次）：
  0 10 * * 1  cd /path/to/cmcc-auto-checkin && python3 probe_jwt.py >> jwt_lifetime.log 2>&1

结果追加到 jwt_lifetime.log，每行：样本标签 | 凭龄(小时) | SUCCESS/FAILED(msg)。
探测成功会触发服务端重签，但实测旧字符串不会被立即作废，不影响其他样本。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from cmcc_sign import USER_AGENT, LegacyTLSAdapter, BASE, Config, SSO_LOGIN
import re
import requests

SAMPLES_FILE = Path(__file__).with_name("jwt_probe_samples.json")
LOG_FILE = Path(__file__).with_name("jwt_lifetime.log")


def probe_one(cfg: Config, jwt: str) -> tuple[bool, str]:
    """用冻结 jwt 尝试一次续期，返回 (是否成功, 说明)。"""
    s = requests.Session()
    s.trust_env = False
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({"user-agent": USER_AGENT})

    r = s.get(SSO_LOGIN, params={"dlwmh": "true", "actUrl": cfg.act_url}, timeout=30)
    m = re.search(r"loginPath\s*=\s*'([^']+)'", r.text)
    if not m:
        return False, "登录页未返回 sid"
    body = {"jwtToken": jwt, "token": "", "provinceCode": cfg.province_code,
            "cityCode": cfg.city_code,
            "userCheckId": format(int(cfg.phone), "x"),
            "carrierOperator": cfg.carrier_operator,
            "appVersionCode": cfg.app_version_code, "took": 300}
    try:
        resp = s.post(BASE + "/qwhdsso" + m.group(1), json=body, timeout=30).json()
    except ValueError as e:
        return False, f"响应解析失败: {e}"
    return resp.get("code") == "SUCCESS", f"{resp.get('code')} {resp.get('msg', '')}".strip()


def main() -> int:
    cfg = Config(str(Path(__file__).with_name("config.json")))
    samples = json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))
    lines = []
    for sm in samples:
        age_h = (time.time() - sm["issued_at"]) / 3600
        ok, detail = probe_one(cfg, sm["jwt"])
        line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} | {sm['label']} | "
                f"凭龄 {age_h:.1f}h | {'SUCCESS' if ok else 'FAILED'} | {detail}")
        lines.append(line)
        print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
