/**
 * muse2api Cookie Import — MV3 Chrome extension
 *
 * Reads muse.ai cookies (including httpOnly) via chrome.cookies and POSTs them to
 * POST {base}/admin/accounts  with Authorization: Bearer <admin key>.
 *
 * Cookies are NEVER stored in extension storage; only the gateway URL and key are saved.
 * Host permissions: muse.ai + optional gateway URL (requested at save time).
 *
 * https://github.com/www222fff/muse2api
 */

"use strict";

const $ = (id) => document.getElementById(id);
const STORE_KEY = "muse2api_ext_cfg";
const LANG_KEY = "muse2api_ext_lang";

// Core httpOnly cookies required for generation.
// hatch_gw / hatch_vml are optional: muse.ai no longer sets them for every session.
const ESSENTIAL = ["hatch_sess", "hatch_native_auth_device"];

// ---- i18n ----
const LANG_STRINGS = {
  en: {
    title: "muse2api Cookie Import",
    sub: "Sync the muse.ai login session from this browser to your muse2api gateway in one click.",
    baseLabel: "Gateway URL (BASE URL)",
    labelLabel: "Account label (optional)",
    btn: "Read and import",
    steps: `<b>Before you start:</b><br>
      1. Logged in to <a href="https://muse.ai/" target="_blank" style="color:#2fbfab">muse.ai</a> in <b>this browser</b>.<br>
      2. Gateway URL and Admin Key filled in (from the muse2api dashboard "Accounts" tab).`,
    foot: `Reads muse.ai cookies via <code>chrome.cookies</code> and POSTs to your gateway. Nothing sent to third parties. Cookies never stored.`,
    noBase: "Please enter the Gateway URL first.",
    noKey: "Please enter the Admin Key first.",
    reading: "Reading muse.ai cookies\u2026",
    noCookies: "No muse.ai cookies found.\nOpen and log in to https://muse.ai/ in this browser first, then click again.",
    missingCore: "Read {n} cookies but missing core: {missing}\nLog in to muse.ai until you can see the chat interface, then try again.",
    uploading: "Read {n} cookies, uploading to {base}\u2026",
    badKey: "Invalid Admin Key (HTTP 401).\nCopy the correct key from the top of the muse2api dashboard.",
    httpError: "Import failed: HTTP {status}\n{body}",
    success: "\u2713 Imported successfully\nLabel: {label}\nAccount ID: {id}\nCookies: {count}\nExpires: {expires}\n",
    warning: "\nNote: {warning}",
    networkError: "Error: {msg}\n\nCommon causes:\n\u00b7 Wrong Gateway URL or gateway not running\n\u00b7 URL is not https (or cert not trusted)\n\u00b7 Browser blocked cross-origin request",
    auto: "(auto)",
    unknown: "unknown",
  },
  vi: {
    title: "muse2api Nh\u1eadp Cookie",
    sub: "Nh\u1eadp cookie \u0111\u0103ng nh\u1eadp muse.ai v\u00e0o gateway muse2api ch\u1ec9 b\u1eb1ng m\u1ed9t c\u00fa nh\u1ea5p.",
    baseLabel: "\u0110\u1ecba ch\u1ec9 gateway (BASE URL)",
    labelLabel: "Nh\u00e3n t\u00e0i kho\u1ea3n (t\u00f9y ch\u1ecdn)",
    btn: "\u0110\u1ecdc v\u00e0 nh\u1eadp",
    steps: `<b>Tr\u01b0\u1edbc khi b\u1eaft \u0111\u1ea7u:</b><br>
      1. \u0110\u00e3 \u0111\u0103ng nh\u1eadp <a href="https://muse.ai/" target="_blank" style="color:#2fbfab">muse.ai</a> tr\u00ean tr\u00ecnh duy\u1ec7t n\u00e0y.<br>
      2. \u0110i\u1ec1n s\u1eb5n \u0111\u1ecba ch\u1ec9 gateway v\u00e0 Admin Key (l\u1ea5y t\u1eeb tab \u201cAccounts\u201d tr\u00ean dashboard muse2api).`,
    foot: `\u0110\u1ecdc cookie muse.ai qua <code>chrome.cookies</code> v\u00e0 POST l\u00ean gateway c\u1ee7a b\u1ea1n. Kh\u00f4ng g\u1eedi l\u00ean b\u00ean th\u1ee9 ba. Cookie kh\u00f4ng \u0111\u01b0\u1ee3c l\u01b0u trong extension.`,
    noBase: "Vui l\u00f2ng nh\u1eadp \u0111\u1ecba ch\u1ec9 gateway tr\u01b0\u1edbc.",
    noKey: "Vui l\u00f2ng nh\u1eadp Admin Key tr\u01b0\u1edbc.",
    reading: "\u0110ang \u0111\u1ecdc cookie muse.ai\u2026",
    noCookies: "Kh\u00f4ng t\u00ecm th\u1ea5y cookie muse.ai.\nH\u00e3y m\u1edf v\u00e0 \u0111\u0103ng nh\u1eadp https://muse.ai/ tr\u00ean tr\u00ecnh duy\u1ec7t n\u00e0y tr\u01b0\u1edbc.",
    missingCore: "\u0110\u1ecdc \u0111\u01b0\u1ee3c {n} cookie nh\u01b0ng thi\u1ebfu core: {missing}\n\u0110\u0103ng nh\u1eadp muse.ai \u0111\u1ebfn khi th\u1ea5y giao di\u1ec7n chat r\u1ed3i th\u1eed l\u1ea1i.",
    uploading: "\u0110\u1ecdc \u0111\u01b0\u1ee3c {n} cookie, \u0111ang t\u1ea3i l\u00ean {base}\u2026",
    badKey: "Admin Key kh\u00f4ng \u0111\u00fang (HTTP 401).\nSao ch\u00e9p key t\u1eeb dashboard muse2api.",
    httpError: "Nh\u1eadp th\u1ea5t b\u1ea1i: HTTP {status}\n{body}",
    success: "\u2713 Nh\u1eadp th\u00e0nh c\u00f4ng\nNh\u00e3n: {label}\nID: {id}\nS\u1ed1 cookie: {count}\nH\u1ebft h\u1ea1n: {expires}\n",
    warning: "\nL\u01b0u \u00fd: {warning}",
    networkError: "L\u1ed7i: {msg}\n\nNgu\u1ed3n th\u01b0\u1eddng g\u1eb7p:\n\u00b7 \u0110\u1ecba ch\u1ec9 gateway sai ho\u1eb7c gateway ch\u01b0a ch\u1ea1y\n\u00b7 Kh\u00f4ng ph\u1ea3i https\n\u00b7 Tr\u00ecnh duy\u1ec7t ch\u1eb7n y\u00eau c\u1ea7u cross-origin",
    auto: "(t\u1ef1 \u0111\u1ed9ng)",
    unknown: "ch\u01b0a bi\u1ebft",
  },
  zh: {
    title: "muse2api Cookie \u5bfc\u5165",
    sub: "\u4e00\u952e\u628a muse.ai \u767b\u5f55 Cookie \u5bfc\u5165\u5230\u4f60\u7684 muse2api \u7f51\u5173\u3002",
    baseLabel: "\u7f51\u5173\u5730\u5740\uff08BASE URL\uff09",
    labelLabel: "\u8d26\u53f7\u6807\u7b7e\uff08\u53ef\u7559\u7a7a\uff09",
    btn: "\u8bfb\u53d6\u5e76\u5bfc\u5165",
    steps: `<b>\u5f00\u59cb\u524d\u8bf7\u786e\u8ba4\uff1a</b><br>
      1. \u5df2\u5728<b>\u8fd9\u4e2a\u6d4f\u89c8\u5668</b>\u767b\u5f55\u4e86 <a href="https://muse.ai/" target="_blank" style="color:#2fbfab">muse.ai</a>\u3002<br>
      2. \u5df2\u586b\u5199\u7f51\u5173\u5730\u5740\u548c Admin Key\uff08\u4ece muse2api \u7ba1\u7406\u9875\u300c\u8d26\u53f7\u300d\u6807\u7b7e\u9875\u9876\u90e8\u590d\u5236\uff09\u3002`,
    foot: `\u901a\u8fc7 <code>chrome.cookies</code> \u8bfb\u53d6 muse.ai Cookie\uff0cPOST \u5230\u4f60\u586b\u5199\u7684\u7f51\u5173\u3002\u4e0d\u4e0a\u4f20\u5230\u4efb\u4f55\u7b2c\u4e09\u65b9\u3002Cookie \u4e0d\u4f1a\u5b58\u5165\u6269\u5c55\u7a0b\u5e8f\u5b58\u50a8\u3002`,
    noBase: "\u8bf7\u5148\u586b\u5199\u7f51\u5173\u5730\u5740\u3002",
    noKey: "\u8bf7\u5148\u586b\u5199 Admin Key\u3002",
    reading: "\u6b63\u5728\u8bfb\u53d6 muse.ai Cookie\u2026",
    noCookies: "\u6ca1\u6709\u627e\u5230 muse.ai Cookie\u3002\n\u8bf7\u5148\u5728\u8fd9\u4e2a\u6d4f\u89c8\u5668\u767b\u5f55 https://muse.ai/\uff0c\u518d\u56de\u6765\u70b9\u51fb\u3002",
    missingCore: "\u8bfb\u5230 {n} \u6761 Cookie\uff0c\u4f46\u7f3a\u5c11\u6838\u5fc3\u9879\uff1a{missing}\n\u8bf7\u767b\u5f55\u5230\u80fd\u770b\u5230\u804a\u5929\u754c\u9762\u518d\u91cd\u8bd5\u3002",
    uploading: "\u8bfb\u5230 {n} \u6761 Cookie\uff0c\u6b63\u5728\u4e0a\u4f20\u5230 {base}\u2026",
    badKey: "Admin Key \u9519\u8bef\uff08HTTP 401\uff09\u3002\n\u8bf7\u4ece muse2api \u7ba1\u7406\u9875\u590d\u5236\u6b63\u786e\u7684 Key\u3002",
    httpError: "\u5bfc\u5165\u5931\u8d25\uff1aHTTP {status}\n{body}",
    success: "\u2713 \u5bfc\u5165\u6210\u529f\n\u6807\u7b7e\uff1a{label}\nID\uff1a{id}\nCookie \u6570\uff1a{count}\n\u5230\u671f\uff1a{expires}\n",
    warning: "\n\u6ce8\u610f\uff1a{warning}",
    networkError: "\u51fa\u9519\uff1a{msg}\n\n\u5e38\u89c1\u539f\u56e0\uff1a\n\u00b7 \u7f51\u5173\u5730\u5740\u586b\u9519\u6216\u7f51\u5173\u672a\u542f\u52a8\n\u00b7 \u4e0d\u662f https\uff08\u6216\u8bc1\u4e66\u4e0d\u53ef\u4fe1\uff09\n\u00b7 \u6d4f\u89c8\u5668\u62e6\u622a\u4e86\u8de8\u57df\u8bf7\u6c42",
    auto: "(\u81ea\u52a8)",
    unknown: "\u672a\u77e5",
  },
};

// ---- language ----
let lang = "en";
try {
  const saved = localStorage.getItem(LANG_KEY);
  if (saved && LANG_STRINGS[saved]) lang = saved;
  else if (/^zh\b/i.test(navigator.language || "")) lang = "zh";
  else if (/^vi\b/i.test(navigator.language || "")) lang = "vi";
} catch (e) {}

function s(key, vars) {
  const tmpl = LANG_STRINGS[lang][key] || LANG_STRINGS.en[key] || key;
  if (!vars) return tmpl;
  return tmpl.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
}

function applyLang() {
  try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
  $("langSel").value = lang;
  $("t-title").textContent = s("title");
  $("t-sub").textContent = s("sub");
  $("t-base-label").textContent = s("baseLabel");
  $("t-label-label").textContent = s("labelLabel");
  $("go").textContent = s("btn");
  $("t-steps").innerHTML = s("steps");
  $("t-foot").innerHTML = s("foot");
  document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;
}

// ---- log ----
function log(html, cls) {
  const el = $("log");
  el.className = "show";
  el.innerHTML = cls ? `<span class="${cls}">${html}</span>` : html;
}

// ---- URL normalisation ----
function normBase(v) {
  let s = (v || "").trim();
  if (!s) return "";
  if (!/^https?:\/\//i.test(s)) s = "https://" + s;
  s = s.replace(/\/+$/, "").replace(/\/v1$/i, "");
  return s;
}

// ---- config ----
async function loadCfg() {
  const o = await chrome.storage.local.get(STORE_KEY);
  const c = o[STORE_KEY] || {};
  if (c.base) $("base").value = c.base;
  if (c.key) $("key").value = c.key;
  if (c.label) $("label").value = c.label;
  // Auto-detect from active tab if on the dashboard
  if (!c.base) {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const u = tab && tab.url ? new URL(tab.url) : null;
      if (u && /\/dashboard/.test(u.pathname)) {
        $("base").value = u.origin;
        const k = new URLSearchParams(u.search).get("key");
        if (k) $("key").value = k;
      }
    } catch (e) {}
  }
}

async function saveCfg(base, key, label) {
  await chrome.storage.local.set({ [STORE_KEY]: { base, key, label } });
  // Request optional host permission for the gateway URL.
  try {
    const url = new URL(base);
    await chrome.permissions.request({ origins: [`${url.origin}/*`] });
  } catch (e) {}
}

// ---- cookie reading ----
async function grabCookies() {
  // Read all muse.ai cookies including httpOnly.
  const all = await chrome.cookies.getAll({ domain: "muse.ai" });
  const cookies = {}, expiresMap = {};
  for (const c of all) {
    const dom = (c.domain || "").replace(/^\./, "");
    if (!dom.endsWith("muse.ai")) continue;
    cookies[c.name] = c.value;
    if (c.expirationDate) expiresMap[c.name] = Math.floor(c.expirationDate);
  }
  return { cookies, expires: expiresMap };
}

// muse.ai ends a login 30 days after sign-in: the earlier expiry of the two login
// cookies. hatch_vml (48h, re-issued on every renewal) is only a fallback.
function loginExpiry(expires) {
  const login = ["hatch_sess", "hatch_native_auth_device"]
    .map((k) => expires[k]).filter(Boolean);
  return login.length ? Math.min(...login) : (expires["hatch_vml"] || null);
}

// ---- main action ----
async function run() {
  const base = normBase($("base").value);
  const key = $("key").value.trim();
  const label = $("label").value.trim();

  if (!base) { log(s("noBase"), "bad"); return; }
  if (!key) { log(s("noKey"), "bad"); return; }

  $("go").disabled = true;
  log(s("reading"));

  try {
    const { cookies, expires } = await grabCookies();
    const names = Object.keys(cookies);
    if (!names.length) {
      log(s("noCookies"), "bad");
      return;
    }
    const missing = ESSENTIAL.filter((n) => !(n in cookies));
    if (missing.length) {
      log(s("missingCore", { n: names.length, missing: missing.join(", ") }), "warn");
      return;
    }

    log(s("uploading", { n: names.length, base }));

    const origin = new URL(base).origin + "/*";
    const allowed = await chrome.permissions.request({ origins: [origin] });
    if (!allowed) {
      log("Allow the extension to access " + new URL(base).origin + ", then click again.", "bad");
      return;
    }

    const res = await fetch(base + "/admin/accounts", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
      },
      body: JSON.stringify({ label, cookies, cookie_expires: expires }),
    });

    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = {}; }

    if (res.status === 401) {
      log(s("badKey"), "bad");
      return;
    }
    if (!res.ok) {
      const body = typeof data.detail === "string" ? data.detail : text.slice(0, 300);
      log(s("httpError", { status: res.status, body }), "bad");
      return;
    }

    const added = (data.added && data.added[0]) || (data.account) || {};
    await saveCfg(base, key, label);
    const expTs = added.expires_at
      || loginExpiry(expires);
    log(
      s("success", {
        label: added.label || label || s("auto"),
        id: added.id || "?",
        count: added.cookie_count || names.length,
        expires: expTs ? new Date(expTs * 1000).toLocaleString() : s("unknown"),
      })
      + (data.warning ? s("warning", { warning: data.warning }) : ""),
      "ok"
    );
  } catch (e) {
    log(s("networkError", { msg: e && e.message ? e.message : String(e) }), "bad");
  } finally {
    $("go").disabled = false;
  }
}

// ---- init ----
applyLang();
$("langSel").addEventListener("change", (e) => {
  lang = e.target.value;
  applyLang();
});
$("go").addEventListener("click", run);
loadCfg();
