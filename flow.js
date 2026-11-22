// 流量余量 Widget（Scriptable）
// 数据来自 Mac 上 launchd 每 15 分钟生成的 iCloud 文件，Widget 不联网、不碰凭证。
// 用法：新建 Scriptable 小组件 → 选本脚本 → 参数填手机尾号（不填默认 9819）。
//   小号 剩余总量 + 百分比环
//   中号 总量 + 前 3 个套餐（各自剩余/总量与有效期）+ 刚失效的 1 个
//   大号 总量 + 日均/可用天数 + 全部套餐（各自剩余/总量与有效期）
// 已用尽的包不展示（它只是没到期，不贡献剩余量），大号末尾提示还有几个。

const TAIL = (args.widgetParameter || "9819").trim();
const FAMILY = config.widgetFamily || "medium";
const MAX_ROWS = FAMILY === "small" ? 0 : FAMILY === "large" ? 99 : 3;

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

function ago(sec) {
  if (!sec) return "从未更新";
  if (sec < 0) return "刚刚";
  if (sec < 90) return "刚刚";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.round(sec / 3600) + " 小时前";
  return Math.round(sec / 86400) + " 天前";
}

function readJSON(path) {
  const fm = FileManager.iCloud();

  if (!fm.fileExists(path)) return null;
  if (!fm.isFileDownloaded(path)) fm.downloadFileFromiCloud(path);
  return JSON.parse(fm.readString(path));
}

function arcPoints(c, r, sweep, steps) {
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const a = -Math.PI / 2 + (sweep * i) / steps;
    pts.push(new Point(c + r * Math.cos(a), c + r * Math.sin(a)));
  }
  return pts;
}

// Path 没有 addArc：整圈用 strokeEllipse，进度弧用 addLines 折线近似。
function ring(size, ratio, color) {
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
    p.addLines(arcPoints(size / 2, (size - inset * 2) / 2, sweep, Math.max(10, Math.round(sweep / 0.06))));
    dc.addPath(p);
    dc.setStrokeColor(new Color(color));
    dc.setLineWidth(lw);
    dc.strokePath();
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

function header(w, data, ratio, age, size) {
  const stale = data.ok === false || age > 3600;
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  try {
    const img = row.addImage(ring(size, ratio, stale ? S.warn : S.ok));
    img.imageSize = new Size(size, size);
    row.addSpacer(10);
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

function packageRow(w, p, tall) {
  const row = w.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
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

  header(w, data, ratio, age, FAMILY === "small" ? 50 : FAMILY === "large" ? 66 : 58);

  const pkgs = data.packages || [];
  // 快照可能是几小时前的（Mac 抓取失败时保留上一份），已过期的包不能再当成"在用"展示
  const nowMs = Date.now();
  const live = pkgs.filter((p) => p.remain_mb > 0 && !(p.expire && Date.parse(p.expire.replace(" ", "T")) < nowMs));
  const drained = pkgs.length - live.length;

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
    for (const p of live.slice(0, MAX_ROWS)) {
      packageRow(w, p, FAMILY === "large");
      w.addSpacer(FAMILY === "large" ? 4 : 3);
    }
    if (live.length > MAX_ROWS) {
      label(w, `还有 ${live.length - MAX_ROWS} 个`, Font.systemFont(10), C.faint);
    }
    if (FAMILY === "large" && drained > 0) {
      label(w, `另有 ${drained} 个已用完（未到期）`, Font.systemFont(10), C.faint);
    }
    if (data.expired_last) {
      label(w, `刚失效：${shortName(data.expired_last.name)} ${fmtMB(data.expired_last.total_mb)}`,
            Font.systemFont(9), C.faint);
    }
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
  return w;
}

const fm = FileManager.iCloud();

// flow_bg_<尾号>.jpg 放的是与桌面对应区域的壁纸截图裁片：iOS 不允许小组件真透明，
// 只有把壁纸裁片当背景图才能"看起来透明"。没有这张图就退回纯色底。
function wallpaperSlice() {
  const bgPath = fm.joinPath(fm.documentsDirectory(), `flow_bg_${TAIL}.jpg`);
  if (!fm.fileExists(bgPath)) return null;
  if (!fm.isFileDownloaded(bgPath)) fm.downloadFileFromiCloud(bgPath);
  return fm.readImage(bgPath);
}

const path = fm.joinPath(fm.documentsDirectory(), `flow_${TAIL}.json`);
let data = null;
try {
  data = readJSON(path);
} catch (e) {
  data = { ok: false, error: String(e), updated_at: 0 };
}

if (!data || !data.total) {
  Script.setWidget(placeholder(
    data ? `还没有数据\n${data.error || ""}\n在 Mac 上先跑 cmcc_flow.py`
         : `还没有 flow_${TAIL}.json\n在 Mac 上先跑一次\npython3 cmcc_flow.py`,
    data ? C.warn : C.dim));
} else {
  Script.setWidget(build(data));
}
Script.complete();
