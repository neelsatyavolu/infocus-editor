// Renders a show's fresh 2026 graphics from <show_dir>/edl.json into <show_dir>/Graphics (ProRes 4444 with alpha).
//   node render_graphics.mjs <show_dir> [--only <id>]
// - TV cards: one per source segment with a "card"; exactly as many frames as the segment, at edl.fps, so each starts
//   and ends on the TV Standby frame.  -> "<segment id> - Monitor.mov"
// - Overlays with a "render" config (dual / lower / follow …), at the overlay's fps (default 29.97). -> "<overlay id>.mov"
// - Placeholder slates for missing packages. -> "<segment id> - Slate.png"
// The package folder comes from edl.json "package", else $INFOCUS_PACKAGE. edl.json "theme" (e.g. "halloween") renders
// the package's seasonal look (graphics.html?theme=…); unset is the regular 2026 look.
import { mkdir, readFile, writeFile, rm } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const onlyIdx = process.argv.indexOf("--only");
const only = onlyIdx > 0 ? process.argv[onlyIdx + 1] : null;
const [showDir] = process.argv.slice(2).filter((a, i, all) => !a.startsWith("--") && all[i - 1] !== "--only");
if (!showDir) { console.error("usage: node render_graphics.mjs <show_dir> [--only <id>]"); process.exit(2); }
const edl = JSON.parse(await readFile(path.join(showDir, "edl.json"), "utf8"));
const home = (p) => p.replace(/^~/, process.env.HOME);
const PACKAGE = home(edl.package ?? process.env.INFOCUS_PACKAGE ?? "");
if (!PACKAGE) { console.error('No package folder: set "package" in edl.json or INFOCUS_PACKAGE'); process.exit(2); }
const GFX_SRC = path.join(PACKAGE, "_Working Files", "Graphics Source");
const THEME = String(edl.theme ?? process.env.INFOCUS_THEME ?? "").trim().toLowerCase();   // as common.py theme_name()
const PAGE = pathToFileURL(path.join(GFX_SRC, "graphics.html")).href + (THEME ? `?theme=${encodeURIComponent(THEME)}` : "");
const { openPage, renderClip } = await import(path.join(GFX_SRC, "render.mjs"));
const OUT = path.join(showDir, "Graphics");
const rate = (f) => (String(f).includes("/") ? f.split("/").reduce((a, b) => a / b) : Number(f));
const expand = (p) => home(p.replace("{PACKAGE}", PACKAGE));
await mkdir(OUT, { recursive: true });

function slateHtml(label, note) {
  const font = (w, f) => `@font-face{font-family:Lexend;font-weight:${w};src:url("${pathToFileURL(path.join(PACKAGE, "02 Fonts", f)).href}")}`;
  return `<!doctype html><meta charset="utf-8"><style>${font(400, "Lexend-Regular.ttf")}${font(600, "Lexend-SemiBold.ttf")}${font(700, "Lexend-Bold.ttf")}
  html,body{margin:0;width:1920px;height:1080px;background:#0F110F;font-family:Lexend}
  .b{position:absolute;left:120px;top:452px;width:12px;height:176px;background:#EE3A2A}
  .k{position:absolute;left:170px;top:440px;color:#2BB36E;font-weight:700;font-size:30px;letter-spacing:.12em}
  .t{position:absolute;left:170px;top:488px;color:#ECEFEA;font-weight:600;font-size:84px}
  .n{position:absolute;left:170px;top:600px;color:#9AA39C;font-size:40px}</style>
  <div class=b></div><div class=k>PLACEHOLDER · PACKAGE</div><div class=t>${label}</div><div class=n>${note}</div>`;
}

// Playwright's own Chromium (the installer puts it in ~/Library/Caches/ms-playwright), not the package's default of
// the installed Google Chrome: Chrome started by the editor runs its self-updater, which macOS blocks with a
// "uv was prevented from modifying apps" notice, and an editing Mac may not have Chrome at all.
async function openBundledPage() {
  const { chromium } = await import(pathToFileURL(path.join(GFX_SRC, "node_modules", "playwright-core", "index.mjs")).href);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  page.on("console", (m) => { if (m.type() === "warning" || m.type() === "error") console.warn(`[graphics] ${m.text()}`); });
  await page.goto(PAGE);
  return { browser, page };
}

const { browser, page } = await openBundledPage().catch(async (e) => {
  console.warn(`Playwright's Chromium didn't start (${e.message.split("\n")[0]}); using Google Chrome.`);
  const opened = await openPage();
  await opened.page.goto(PAGE);
  return opened;
});
// A themed render needs the theme in the package's graphics.html (its CSS block and assets/<theme>/ logos);
// an older package would quietly render the regular look.
if (THEME && !(await page.evaluate(() => document.documentElement.dataset.theme))) {
  console.error(`The package's graphics.html has no "${THEME}" theme; update the package (Themes/ and Graphics Source).`);
  await browser.close();
  process.exit(3);
}
try {
  for (const seg of edl.segments) {
    if (only && seg.id !== only) continue;
    if (seg.kind === "source" && seg.card) {
      const frames = seg.out - seg.in;
      const c = seg.card;
      const cfg = { type: "announcement", title: c.title.replace(/\s*\|\s*/g, "\n"), points: c.points ?? [], kicker: c.kicker,
        image: c.image ? pathToFileURL(expand(c.image)).href : undefined, duration: (frames / rate(edl.fps)) * 1000 };
      const n = await renderClip(page, cfg, path.join(OUT, `${seg.id} - Monitor.mov`), edl.fps);
      if (n !== frames) throw new Error(`${seg.id}: rendered ${n} frames, expected ${frames}`);
      console.log(`card ${seg.id} (${n} frames)`);
    } else if (seg.kind === "placeholder") {
      const html = path.join(OUT, ".slate.html");
      await writeFile(html, slateHtml(seg.label ?? seg.id, seg.slate_note ?? "Drop the package here. It wasn't in the show recording."));
      const p = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
      await p.goto(pathToFileURL(html).href);
      await p.evaluate(() => document.fonts.ready);
      await p.screenshot({ path: path.join(OUT, `${seg.id} - Slate.png`) });
      await p.close();
      await rm(html);
      console.log(`slate ${seg.id}`);
    }
  }
  for (const ov of edl.overlays ?? []) {
    if (!ov.render || (only && ov.id !== only)) continue;
    await renderClip(page, ov.render, path.join(OUT, `${ov.id}.mov`), ov.fps ?? "30000/1001");
    console.log(`overlay ${ov.id}`);
  }
} finally {
  await browser.close();
}
