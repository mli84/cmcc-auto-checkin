// Runs flow.js against a fake Scriptable + iCloud so the widget's read path is testable
// off-device. Guards the "must await downloadFileFromiCloud" bug: a busy-wait blocks the
// JS thread, the download promise never settles, and the widget eats stale cache for hours.
// Run: node test_flow_widget.mjs
import assert from "node:assert/strict";
import fs from "node:fs";

const any = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : any),
  set: () => true,
  apply: () => any,
  construct: () => any,
});

function fakeFM(files, dir, opts = {}) {
  return {
    documentsDirectory: () => dir,
    joinPath: (a, b) => `${a}/${b}`,
    fileExists: (p) => p in files,
    isFileDownloaded: () => true,
    downloadFileFromiCloud: (p) =>
      new Promise((resolve) =>
        setTimeout(() => {
          if (!opts.never && !files[p]) files[p] = opts.freshRaw;
          resolve();
        }, 5)
      ),
    readString: (p) => files[p] ?? null,
    writeString: (p, s) => { files[p] = s; },
    listContents: (d) => Object.keys(files).map((p) => p.slice(d.length + 1)),
    readImage: () => null,
  };
}

const src = fs.readFileSync(new URL("./flow.js", import.meta.url), "utf8");

async function runWidget(files, opts) {
  const g = {
    FileManager: {
      iCloud: () => fakeFM(files, "/iCloud", opts),
      local: () => fakeFM(files, "/local", opts),
    },
    Device: { isDarkMode: false },
    Color: any, Font: any, ListWidget: any, Size: any, Point: any, DrawContext: any, Script: any,
    args: { widgetParameter: "9819" },
    config: { widgetFamily: "medium" },
  };
  await new Function(...Object.keys(g), `return (async () => { ${src} })()`)(...Object.values(g));
  return files["/iCloud/flow_widget_9819.log"];
}

const fresh = JSON.stringify({
  updated_at: Math.floor(Date.now() / 1000),
  ok: true,
  total: { total_mb: 2000, used_mb: 766, remain_mb: 1234 },
  packages: [{ id: "g", name: "包", total_mb: 2000, used_mb: 766, remain_mb: 1234, expire: "2099-01-01 00:00:00" }],
});
const stale = JSON.stringify({
  updated_at: 1, ok: true, total: { total_mb: 2000, used_mb: 1001, remain_mb: 999 }, packages: [],
});

// Remote-only file: download lands a few ms later. Only an awaited download can read it.
let log = await runWidget({ "/iCloud/flow_9819.json": null }, { freshRaw: fresh });
assert.match(log, /OK remain=1234 .*raw=\d+ tries=1/, "must render fresh data read after awaited download");
assert.match(log, /v3/, "log carries script version so the phone's copy is identifiable");

// Mac-side rotation: the canonical file is a wedged 0-byte copy on the phone, but a newer
// timestamped snapshot is readable. The widget must render the newest snapshot, not the stale one.
const newer = JSON.stringify({
  updated_at: Math.floor(Date.now() / 1000),
  ok: true,
  total: { total_mb: 2000, used_mb: 766, remain_mb: 4321 },
  packages: [{ id: "g", name: "包", total_mb: 2000, used_mb: 766, remain_mb: 4321, expire: "2099-01-01 00:00:00" }],
});
log = await runWidget({
  "/iCloud/flow_9819.json": "",
  "/iCloud/flow_9819_1795485630123.json": newer,
  "/iCloud/flow_9819_1795484690123.json": stale,
});
assert.match(log, /OK remain=4321 .*src=flow_9819_1795485630123\.json/, "newest timestamped snapshot wins");

// Newest snapshot unreadable (still downloading): fall back to the next one, not the canonical.
log = await runWidget({
  "/iCloud/flow_9819_1795485630123.json": "",
  "/iCloud/flow_9819_1795484690123.json": fresh,
});
assert.match(log, /OK remain=1234 .*src=flow_9819_1795484690123\.json/, "falls back to the next snapshot");

// Download never yields content: fall back to the local cache instead of a placeholder.
log = await runWidget(
  { "/iCloud/flow_9819.json": null, "/local/flow_9819.cache.json": stale },
  { freshRaw: fresh, never: true }
);
assert.match(log, /remain=999 .*raw=0 tries=3/, "exhausts retries then uses cache");
assert.match(log, /cache/, "cache fallback is marked in the log");

console.log("flow widget checks OK");
