// 流量余量 Widget（Scriptable）
// 数据来自 Mac 上 launchd 每 15 分钟生成的 iCloud 文件，Widget 不联网、不碰凭证。
// 用法：新建 Scriptable 小组件 → 选本脚本 → 参数填手机尾号（不填默认 9819）。
//   小号 剩余总量 + 百分比环
//   中号 总量进度环（弧长=整体可用比例，圆心写同一比例）+ 右侧彩色分段环（各包剩余量占比）+ 前 3 个套餐（圆点与环同色）+ 最近失效/用完的 1 个
//   大号 总量 + 日均/可用天数 + 全部套餐（各自剩余/总量与有效期）
// 已用尽的包不展示（它只是没到期，不贡献剩余量），大号末尾提示还有几个。

const TAIL = (args.widgetParameter || "9819").trim();
const FAMILY = config.widgetFamily || "medium";
const MAX_ROWS = FAMILY === "small" ? 0 : FAMILY === "large" ? 99 : 3;
const fm = FileManager.iCloud();
// iCloud 偶发「文件在、读出来 0 字节」（日志里 exists=true raw=0）：本地再留一份上次成功的
// 快照兜底，宁可显示旧数据（头部「x 分钟前」会标黄）也不挂占位提示。
const localFm = FileManager.local();
const CACHE = localFm.joinPath(localFm.documentsDirectory(), `flow_${TAIL}.cache.json`);

// 深浅两套色值：文字/底色用 Color.dynamic 交给系统解析，DrawContext 画的图不参与解析，
// 所以另留一份按 Device.isDarkMode 取值的纯色表 S。
const LIGHT = {
  bg: "#f2f5f8", text: "#101418", dim: "#5a6674", faint: "#8b97a6",
  line: "#2a3340", track: "#dde4ec", ok: "#16a34a", warn: "#dc2626",
};
const DARK = {
  bg: "#101418", text: "#f2f5f8", dim: "#8b9bb0", faint: "#4a5768",
  line: "#d5dde7", track: "#2a3340", ok: "#4ade80", warn: "#f87171",
};
const S = Device.isDarkMode ? DARK : LIGHT;
const C = {};
for (const k in DARK) C[k] = Color.dynamic(new Color(LIGHT[k]), new Color(DARK[k]));

// 中号环里每个包一个颜色，与下方包名前的圆点一一对应；超出调色板的包归入灰色「其它」。
const SEG = Device.isDarkMode
  ? ["#60a5fa", "#4ade80", "#fbbf24", "#f472b6", "#a78bfa", "#22d3ee"]
  : ["#2563eb", "#16a34a", "#d97706", "#db2777", "#7c3aed", "#0891b2"];
const SEG_REST = Device.isDarkMode ? "#64748b" : "#94a3b8";

function fmtMB(mb) {
  if (mb == null) return "—";
  return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : Math.round(mb) + " MB";
}

function shortName(name) {
  return String(name || "").replace(/\s*[（(]\d{4}\/\d{1,2}\/\d{1,2}.*$/, "").replace(/（已失效）/, "");
}

function expiry(dateStr) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(dateStr || "");
  if (!m) return "";
  const hm = m[4] ? ` ${m[4]}:${m[5]}` : "";
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  const today = new Date();
  const diff = Math.round((d - new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
  if (diff === 0) return `今天${hm} 到期`;
  if (diff === 1) return `明天${hm} 到期`;
  return `${+m[2]}-${+m[3]}${hm} 到期`;
}

function pastMoment(dateStr, suffix) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(dateStr || "");
  if (!m) return "";
  const hm = m[4] ? ` ${m[4]}:${m[5]}` : "";
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  const today = new Date();
  const diff = Math.round((d - new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
  if (diff === 0) return `今天${hm} ${suffix}`;
  if (diff === -1) return `昨天${hm} ${suffix}`;
  return `${+m[2]}-${+m[3]}${hm} ${suffix}`;
}

function stampOf(sec, withSec) {
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}` + (withSec ? ":" + p(d.getSeconds()) : "");
}

// 接口只有"失效时间"，"用完时间"是 Mac 首次看到归零的那次快照，两者比新旧取最近的一个。
function recentLine(data) {
  const gone = data.expired_last;
  const done = data.drained_last;
  const goneMs = gone ? Date.parse(String(gone.expire || "").replace(" ", "T")) || 0 : 0;
  const doneMs = done && done.at ? done.at * 1000 : 0;
  // 旧快照可能把"到期作废"记成"用完"（且没写 expire）；包已过期就不再信这条记录。
  const spent = done && done.expire &&
                (Date.parse(String(done.expire).replace(" ", "T")) || 0) >= Date.now();
  if (spent && doneMs > goneMs) {
    return `已用完：${shortName(done.name)} ${fmtMB(done.total_mb)} · ${pastMoment(stampOf(done.at), "用完")}`;
  }
  if (gone) {
    const t = pastMoment(gone.expire, "失效");
    return `刚失效：${shortName(gone.name)} ${fmtMB(gone.total_mb)}${t ? " · " + t : ""}`;
  }
  return "";
}

function ago(sec) {
  if (!sec) return "从未更新";
  if (sec < 0) return "刚刚";
  if (sec < 90) return "刚刚";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.round(sec / 3600) + " 小时前";
  return Math.round(sec / 86400) + " 天前";
}

// Widget 跑在手机上，Mac 看不到它的日志，所以把每次读取结果写进 iCloud 同目录的
// flow_widget_<尾号>.log，Mac 上直接 tail 就能判断是「文件没同步过来」还是「下载失败」。
function logLine(text) {
  try {
    const p = fm.joinPath(fm.documentsDirectory(), `flow_widget_${TAIL}.log`);
    let old = "";
    if (fm.fileExists(p)) {
      if (!fm.isFileDownloaded(p)) fm.downloadFileFromiCloud(p);
      old = fm.readString(p) || "";
    }
    // 只留最近一段，15 分钟一次也不会把 iCloud 撑大
    if (old.length > 16000) old = old.slice(-12000);
    fm.writeString(p, `${old}${stampOf(Date.now() / 1000, true)} ${FAMILY} v3 ${text}\n`);
  } catch (e) {}
}

// downloadFileFromiCloud 是异步的（返回 Promise）。以前用忙等「等」下载落地，JS 线程
// 被占住，Promise 永远没机会 resolve，文件就一直读不出来——日志里 raw=0 能卡几个小时，
// Widget 只能吃本地缓存。必须 await，让下载真的完成。
async function readJSON(path) {
  const info = { exists: false, downloaded: false, dl: "skip", raw: 0, tries: 0 };
  for (let attempt = 1; attempt <= 3; attempt++) {
    info.tries = attempt;
    info.exists = fm.fileExists(path);
    info.downloaded = info.exists && fm.isFileDownloaded(path);
    try {
      // isFileDownloaded 会在文件没落地时也报 true，此时 readString 静默返回 null，
      // JSON.parse(null) === null 会被误判成「文件不存在」；所以每次读之前都强制下载一次
      await fm.downloadFileFromiCloud(path);
      info.dl = "ok";
    } catch (e) {
      info.dl = "dl:" + String(e).slice(0, 80);
    }
    let raw = null;
    try {
      raw = fm.readString(path);
    } catch (e) {
      info.dl += " read:" + String(e).slice(0, 60);
    }
    info.raw = raw ? raw.length : 0;
    if (raw) {
      try {
        const d = JSON.parse(raw);
        if (d && d.total) {
          try { localFm.writeString(CACHE, raw); } catch (e) {}
          return { data: d, info };
        }
        info.dl += " shape:" + (d && typeof d === "object" ? Object.keys(d).join("/") : String(d)).slice(0, 60);
      } catch (e) {
        info.dl += " parse:" + String(e).slice(0, 80);
      }
    }
  }
  return { data: null, info };
}

function arcPoints(c, r, start, sweep, steps) {
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const a = start + (sweep * i) / steps;
    pts.push(new Point(c + r * Math.cos(a), c + r * Math.sin(a)));
  }
  return pts;
}

// Path 没有 addArc：整圈用 strokeEllipse，进度弧用 addLines 折线近似。
// showPct=true（仅中号）时把同一个比例写进圆心，弧长与数字同源，不会各算各的。
function ring(size, ratio, color, showPct) {
  const dc = new DrawContext();
  dc.size = new Size(size, size);
  dc.opaque = false;
  dc.respectScreenScale = true;
  const lw = Math.max(4, Math.round(size / 9));
  const inset = lw / 2 + 1;
  dc.setLineWidth(lw);
  dc.setStrokeColor(new Color(S.track));
  dc.strokeEllipse(new Rect(inset, inset, size - inset * 2, size - inset * 2));
  const sweep = Math.PI * 2 * Math.min(Math.max(ratio, 0), 1);
  if (sweep > 0.05) {
    const p = new Path();
    p.addLines(arcPoints(size / 2, (size - inset * 2) / 2, -Math.PI / 2, sweep, Math.max(10, Math.round(sweep / 0.06))));
    dc.addPath(p);
    dc.setStrokeColor(new Color(color));
    dc.setLineWidth(lw);
    dc.strokePath();
  }
  if (showPct) {
    const c = size / 2;
    dc.setTextAlignedCenter();
    dc.setTextColor(new Color(S.text));
    dc.setFont(Font.boldSystemFont(13));
    dc.drawTextInRect(`${Math.round(Math.min(Math.max(ratio, 0), 1) * 100)}%`, new Rect(0, c - 17, size, 16));
    dc.setTextColor(new Color(S.dim));
    dc.setFont(Font.systemFont(8));
    dc.drawTextInRect("可用", new Rect(0, c + 1, size, 10));
  }
  return dc.getImage();
}

// 中号：按各包剩余量占比把整圈切成彩色扇段（纯分布，不再写中心比例，比例已在中号左环圆心）。
function segmentRing(size, segs) {
  const dc = new DrawContext();
  dc.size = new Size(size, size);
  dc.opaque = false;
  dc.respectScreenScale = true;
  const lw = Math.max(4, Math.round(size / 9));
  const inset = lw / 2 + 1;
  const r = (size - inset * 2) / 2;
  const c = size / 2;
  dc.setLineWidth(lw);
  if (!segs.length) {
    dc.setStrokeColor(new Color(S.track));
    dc.strokeEllipse(new Rect(inset, inset, size - inset * 2, size - inset * 2));
  }
  const total = segs.reduce((s, x) => s + x.ratio, 0) || 1;
  const gap = segs.length > 1 ? 0.05 : 0;
  let start = -Math.PI / 2;
  for (const s of segs) {
    const full = (Math.PI * 2 * s.ratio) / total;
    const sweep = full - gap;
    if (sweep > 0.02) {
      const p = new Path();
      p.addLines(arcPoints(c, r, start + gap / 2, sweep, Math.max(6, Math.round(sweep / 0.06))));
      dc.addPath(p);
      dc.setStrokeColor(new Color(s.hex));
      dc.setLineWidth(lw);
      dc.strokePath();
    }
    start += full;
  }
  return dc.getImage();
}

function label(stack, text, font, color) {
  const t = stack.addText(text);
  t.font = font;
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.7;
  return t;
}

function header(w, data, ratio, age, size, segs) {
  const stale = data.ok === false || age > 3600;
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  try {
    const img = row.addImage(ring(size, ratio, stale ? S.warn : S.ok, !!segs));
    img.imageSize = new Size(size, size);
    row.addSpacer(10);
    if (segs) {
      const simg = row.addImage(segmentRing(size, segs));
      simg.imageSize = new Size(size, size);
      row.addSpacer(10);
    }
  } catch (e) {
    row.addSpacer(0);
  }
  const col = row.addStack();
  col.layoutVertically();
  col.spacing = 0;
  label(col, "剩余流量", Font.mediumSystemFont(11), C.dim);
  label(col, fmtMB(data.total.remain_mb), Font.boldSystemFont(FAMILY === "small" ? 22 : 26), C.text);
  label(col, `${Math.round(ratio * 100)}% · ${ago(age)}`,
        Font.systemFont(10), stale ? C.warn : C.dim);
}

function packageRow(w, p, tall, hex) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  if (hex) {
    const dot = row.addText("●");
    dot.font = Font.systemFont(7);
    dot.textColor = new Color(hex);
    row.addSpacer(4);
  }
  label(row, shortName(p.name), Font.systemFont(tall ? 12 : 11), C.line);
  row.addSpacer(6);
  label(row, `${fmtMB(p.remain_mb)}/${fmtMB(p.total_mb)}`, Font.mediumSystemFont(tall ? 12 : 11), C.text);
  label(row, " · " + expiry(p.expire), Font.systemFont(10), C.faint);
}

function build(data) {
  const w = new ListWidget();
  w.setPadding(12, 14, 12, 14);
  const bg = wallpaperSlice();
  if (bg) w.backgroundImage = bg;
  else w.backgroundColor = C.bg;
  const remain = data.total.remain_mb || 0;
  const total = data.total.total_mb || 0;
  const ratio = total ? remain / total : 0;
  const age = Math.round(Date.now() / 1000 - data.updated_at);

  const pkgs = data.packages || [];
  // 快照可能是几小时前的（Mac 抓取失败时保留上一份），已过期的包不能再当成"在用"展示
  const nowMs = Date.now();
  const live = pkgs.filter((p) => p.remain_mb > 0 && !(p.expire && Date.parse(p.expire.replace(" ", "T")) < nowMs));
  const drained = pkgs.length - live.length;

  // 分段环只给中号：前 MAX_ROWS 个包各占一色，其余包合并成灰色「其它」，整圈正好 100%。
  let segs = null;
  let restShare = 0;
  if (FAMILY === "medium" && live.length) {
    const liveRemain = live.reduce((s, p) => s + p.remain_mb, 0);
    const top = live.slice(0, MAX_ROWS);
    const topRemain = top.reduce((s, p) => s + p.remain_mb, 0);
    segs = top.map((p, i) => ({ ratio: p.remain_mb / liveRemain, hex: SEG[i % SEG.length] }));
    if (liveRemain > topRemain) {
      restShare = Math.round(((liveRemain - topRemain) / liveRemain) * 100);
      segs.push({ ratio: (liveRemain - topRemain) / liveRemain, hex: SEG_REST });
    }
  }

  header(w, data, ratio, age, FAMILY === "small" ? 50 : FAMILY === "large" ? 66 : 58, segs);

  if (MAX_ROWS) {
    if (FAMILY === "large" && data.avg_daily_mb_7d) {
      w.addSpacer(4);
      const left = remain > 0 ? Math.floor(remain / data.avg_daily_mb_7d) : 0;
      label(w, `近 7 天日均 ${fmtMB(data.avg_daily_mb_7d)} · 按此速度约 ${left} 天`,
            Font.systemFont(10), C.dim);
    }
    w.addSpacer(FAMILY === "large" ? 8 : 6);
    if (!live.length) {
      label(w, "没有可用流量包", Font.mediumSystemFont(12), C.warn);
    }
    for (const [i, p] of live.slice(0, MAX_ROWS).entries()) {
      packageRow(w, p, FAMILY === "large", FAMILY === "medium" ? SEG[i % SEG.length] : null);
      w.addSpacer(FAMILY === "large" ? 4 : 3);
    }
    if (live.length > MAX_ROWS) {
      const more = `还有 ${live.length - MAX_ROWS} 个`;
      label(w, restShare ? `${more} · ${restShare}%` : more, Font.systemFont(10), C.faint);
    }
    if (FAMILY === "large" && drained > 0) {
      label(w, `另有 ${drained} 个已用完（未到期）`, Font.systemFont(10), C.faint);
    }
    const recent = recentLine(data);
    if (recent) label(w, recent, Font.systemFont(9), C.faint);
  }

  if (data.ok === false) {
    label(w, "更新失败：" + (data.error || "未知"), Font.systemFont(9), C.warn);
  }
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
  return w;
}

function placeholder(text, color) {
  const w = new ListWidget();
  const bg = wallpaperSlice();
  if (bg) w.backgroundImage = bg;
  else w.backgroundColor = C.bg;
  w.setPadding(12, 14, 12, 14);
  const t = w.addText(text);
  t.font = Font.systemFont(11);
  t.textColor = color;
  // 占位提示也要请求短刷新，否则一次踩空会一直停在这句话上
  w.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);
  return w;
}

// flow_bg_<尾号>.jpg 放的是与桌面对应区域的壁纸截图裁片：iOS 不允许小组件真透明，
// 只有把壁纸裁片当背景图才能"看起来透明"。没有这张图就退回纯色底。
function wallpaperSlice() {
  const bgPath = fm.joinPath(fm.documentsDirectory(), `flow_bg_${TAIL}.jpg`);
  if (!fm.fileExists(bgPath)) return null;
  if (!fm.isFileDownloaded(bgPath)) fm.downloadFileFromiCloud(bgPath);
  return fm.readImage(bgPath);
}

function readCache() {
  try {
    const d = JSON.parse(localFm.readString(CACHE) || "");
    return d && d.total ? d : null;
  } catch (e) {
    return null;
  }
}

// Mac 每轮把快照写成新文件名（flow_<尾号>_<epoch>.json）。同一个文件反复原地覆盖时，
// 手机上的 iCloud 本地副本会卡在 0 字节、download 变成空操作（日志 raw=0），换新文件才会重新下载。
// 固定名 flow_<尾号>.json 仍作为兜底，兼容手机上还没更新的 flow.js。
function snapshotPaths() {
  const dir = fm.documentsDirectory();
  const prefix = `flow_${TAIL}_`;
  let names = [];
  try {
    names = fm.listContents(dir);
  } catch (e) {}
  const newest = names
    .map((n) => String(n).replace(/\.icloud$/, ""))
    .filter((n) => n.startsWith(prefix) && n.endsWith(".json") &&
                   /^\d+$/.test(n.slice(prefix.length, -5)))
    .sort()
    .reverse()
    .slice(0, 2)
    .map((n) => fm.joinPath(dir, n));
  newest.push(fm.joinPath(dir, `flow_${TAIL}.json`));
  return newest;
}

let data = null;
let info = {};
try {
  for (const p of snapshotPaths()) {
    ({ data, info } = await readJSON(p));
    info.src = p.slice(p.lastIndexOf("/") + 1);
    if (data && data.total) break;
  }
} catch (e) {
  data = { ok: false, error: String(e), updated_at: 0 };
  info = { dl: "throw:" + String(e).slice(0, 100) };
}

if (!data || !data.total) {
  const cached = readCache();
  if (cached) {
    data = cached;
    info.src = "cache";
    info.dl += " cache";
  }
}

if (!data || !data.total) {
  let dir = "";
  try {
    dir = fm.listContents(fm.documentsDirectory())
           .filter((n) => n.startsWith("flow_") && n.includes(".json")).join(",");
  } catch (e) {}
  logLine(`MISS src=${info.src || "-"} exists=${info.exists} downloaded=${info.downloaded} raw=${info.raw} tries=${info.tries} dl=${info.dl} dir=[${dir}]`);
  Script.setWidget(placeholder(
    data ? `还没有数据\n${data.error || ""}\n在 Mac 上先跑 cmcc_flow.py`
      : info.exists ? "iCloud 文件还没同步下来\n稍后自动重试"
      : `还没有 flow_${TAIL}.json\n在 Mac 上先跑一次\npython3 cmcc_flow.py`,
    data ? C.warn : C.dim));
} else {
  logLine(`OK remain=${data.total.remain_mb} updated_at=${data.updated_at} src=${info.src || "-"} raw=${info.raw} tries=${info.tries} dl=${info.dl}`);
  Script.setWidget(build(data));
}
Script.complete();
