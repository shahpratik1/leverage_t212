import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "trading_system" / "dashboard" / "assets"
STYLES_PATH = ASSETS_DIR / "styles.css"
WORKBENCH_JS_PATH = ASSETS_DIR / "workbench.js"


def _find_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    homebrew_node = Path("/opt/homebrew/bin/node")
    if homebrew_node.exists():
        return str(homebrew_node)
    raise unittest.SkipTest("Node.js runtime not found for JS asset verification")


class TestWorkbenchAssetsStatic(unittest.TestCase):
    """Static security, hygiene, compatibility, and contract checks on U6 assets."""

    def setUp(self) -> None:
        self.assertTrue(STYLES_PATH.exists(), f"Missing {STYLES_PATH}")
        self.assertTrue(WORKBENCH_JS_PATH.exists(), f"Missing {WORKBENCH_JS_PATH}")
        self.styles_text = STYLES_PATH.read_text(encoding="utf-8")
        self.js_text = WORKBENCH_JS_PATH.read_text(encoding="utf-8")

    def test_node_syntax_valid(self) -> None:
        node = _find_node()
        res = subprocess.run(
            [node, "--check", str(WORKBENCH_JS_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"node --check failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )

    def test_zero_external_cdn_or_remote_urls(self) -> None:
        svg_ns = "http://www.w3.org/2000/svg"
        for label, content in [("styles.css", self.styles_text), ("workbench.js", self.js_text)]:
            matches = [
                m
                for m in re.findall(r"https?://[^\s'\"`)]+", content, flags=re.IGNORECASE)
                if m != svg_ns
            ]
            self.assertEqual(
                matches,
                [],
                f"{label} must contain zero external http(s) URLs, found: {matches}",
            )
            self.assertNotIn("@import url(", content, f"{label} must not use external CSS @import")

    def test_zero_unsafe_dom_html_sinks(self) -> None:
        forbidden = [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function(",
        ]
        for sink in forbidden:
            self.assertNotIn(
                sink,
                self.js_text,
                f"workbench.js must not use unsafe sink {sink!r}",
            )

    def test_legacy_hardcoded_copy_removed(self) -> None:
        legacy_strings = [
            "Connors 3-RSI",
            "84% EUR",
            "120 Assets",
            "LIVE AUTO-SYNC (3s)",
        ]
        for legacy in legacy_strings:
            self.assertNotIn(legacy, self.js_text, f"Legacy string {legacy!r} found in workbench.js")
            self.assertNotIn(legacy, self.styles_text, f"Legacy string {legacy!r} found in styles.css")

    def test_no_polling_interval_and_no_local_storage(self) -> None:
        # Must NOT poll /api/data every 3.5s
        self.assertNotRegex(self.js_text, r"setInterval\s*\([\s\S]{0,200}3500")
        # Auth token must strictly live in sessionStorage, never localStorage
        self.assertNotIn("localStorage", self.js_text)
        self.assertIn("sessionStorage", self.js_text)

    def test_preserves_equity_compatibility_and_routes_and_aria(self) -> None:
        for marker in [
            "equity-chart-svg",
            "equity-chart-group",
            "equity-gradient-emerald",
            "equity-gradient-rose",
            "equity-val-current",
            "renderEquityChart",
            "equity_history",
            "INITIAL_DATA",
        ]:
            self.assertIn(marker, self.js_text, f"Missing compatibility marker {marker!r}")

        for route in ["monitor", "config", "audit"]:
            self.assertIn(f"#{route}", self.js_text)
            self.assertIn(f"workspace-{route}", self.js_text)

        for aria_token in ['role: "tablist"', 'role: "tab"', 'role: "tabpanel"', 'role: "dialog"', '"aria-modal": "true"']:
            self.assertIn(aria_token, self.js_text)

    def test_backend_endpoint_contracts_present(self) -> None:
        required_endpoints = [
            "/api/data",
            "/stream",
            "/api/auth/verify",
            "/api/config/preview",
            "/api/config",
            "/api/params",
            "/re-epoch/preview",
            "/re-epoch-all/preview",
            "/re-epoch-all",
            "/api/panic",
            "/api/resume",
            "X-Dashboard-Token",
        ]
        for ep in required_endpoints:
            self.assertIn(ep, self.js_text, f"Missing backend endpoint/header {ep!r}")

    def test_responsive_css_and_focus_accessibility(self) -> None:
        self.assertIn(":focus-visible", self.styles_text)
        self.assertIn("@media (max-width: 767px)", self.styles_text)
        self.assertIn("@media (min-width: 768px)", self.styles_text)
        self.assertIn(".wb-diff-drawer", self.styles_text)
        self.assertIn(".wb-modal", self.styles_text)

    def test_mission_control_and_simple_mode_markers(self) -> None:
        markers = [
            "renderMissionControlCard",
            "workflow-stepper",
            "PLAIN_ENGLISH_GATE_TRANSLATIONS",
            "translateGateRejection",
            "renderQuickControls",
            "quick-controls-card",
            "header-view-mode-btn",
            "next-scan-countdown",
            "next-monitor-countdown",
        ]
        for m in markers:
            self.assertIn(m, self.js_text, f"Missing Mission Control marker {m!r} in workbench.js")

        self.assertIn('"vwap"', self.js_text, "Missing 'vwap' strategy option in workbench.js")
        self.assertIn("VWAP Session Trend", self.js_text, "Missing 'VWAP Session Trend' label in workbench.js")

        css_markers = [
            ".wb-mc-card",
            ".wb-stepper",
            ".wb-step--active",
            ".wb-step--completed",
            ".wb-mc-now-box",
            ".wb-quick-controls",
            ".wb-segmented-control",
            ".wb-tracker-tray",
        ]
        for c in css_markers:
            self.assertIn(c, self.styles_text, f"Missing CSS class {c!r} in styles.css")


_NODE_HARNESS_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const workbenchCode = fs.readFileSync(process.env.WORKBENCH_JS_PATH, "utf8");

function makeSampleInitialData(overrides = {}) {
  const base = {
    generated_at: "2026-11-11T12:00:00Z",
    controller_attached: true,
    read_only_reason: "",
    system_halted: false,
    halt_reason: "",
    config_epoch_id: "epoch-1",
    pending_restart_keys: [],
    config_quarantine_warnings: [],
    base_equity: 10000,
    current_equity: 10350,
    equity_history: [
      { equity: 10000, timestamp: "2026-11-11T09:00:00Z" },
      { equity: 10350, timestamp: "2026-11-11T12:00:00Z" },
    ],
    broker_status: {
      mode: "PRACTICE",
      currency: "EUR",
      equity: 10350,
      free_cash: 7200,
      pnl: 350,
      open_positions_count: 1,
    },
    rate_limit_telemetry: {
      wait_count: 2,
      rate_limit_429_count: 1,
      global_cooldown_remaining: 0,
    },
    active_signals: [
      {
        ticker: "3LNV",
        display_name: "GraniteShares 3x Long NVIDIA",
        direction: "LONG",
        shares: 25,
        entry_price: 40.0,
        current_price: 42.5,
        stop_price: 38.0,
        pnl_eur: 62.5,
        pnl_pct: 6.25,
        config_epoch_id: "epoch-0",
        status: "PROTECTED",
        tsl_mode: "PHASED_DECAY",
      },
    ],
    recent_notifications: [
      { timestamp: "2026-11-11T11:45:00Z", ticker: "3LNV", title: "Entry filled", message: "Epoch #epoch-0" },
    ],
    pairs_watchlist: [
      { underlying: "NVDA", pair_status: "ACTIVE", long_etp: "3LNV", short_etp: "3SNV", adx: 29.5 },
    ],
    funnel: {
      stage_counts: {
        universe: 40,
        screened: 18,
        strategy: 6,
        debate: 4,
        gates: 2,
        executed: 1,
      },
      gate_rejections: [
        { gate_name: "adx_trend_gate", param_key: "MIN_ADX_TREND", count: 4, structural: false, label: "ADX below threshold" },
        { gate_name: "stale_epoch_superseded", param_key: null, count: 1, structural: true, label: "Epoch superseded during scan" },
      ],
      cross_sectional_metrics: {
        adx: { min: 11.2, p25: 17.5, median: 23.8, p75: 31.4, max: 46.2, count: 18 },
      },
    },
    audit: {
      latest: {
        timestamp: "2026-11-11T11:30:00Z",
        consecutive_zero_cycles: 3,
        health_status: "WARN",
        missing_indicators: ["earnings_in_days"],
        null_metrics_count: 2,
        total_assets_audited: 18,
        mean_adx: 23.8,
        mean_realized_vol: 0.34,
        regime: "TREND",
        recommendations: [
          {
            param_key: "MIN_ADX_TREND",
            suggested_value: 20,
            reason: "Median ADX is 23.8 across 18 screened assets.",
          },
        ],
        action_taken: "FLAGGED_FOR_REVIEW",
      },
      history: [
        { epoch_id: "epoch-1", timestamp: "2026-11-11T09:00:00Z", reason: "Session start", author: "system", changed_keys: [] },
      ],
    },
    config: {
      config_epoch_id: "epoch-1",
      values: {
        MIN_ADX_TREND: 22,
        RISK_PER_TRADE_PCT: 1.0,
        MIN_SL_PCT: 3.0,
        MAX_SL_PCT: 6.0,
        PURE_TSL_MODE: false,
        TRADE_WINDOW_START: "09:00",
        TRADE_CUTOFF: "16:30",
        MIN_PRICE_USD: 5.0,
      },
      dynamic_parameters: {},
    },
    schema: {
      parameters: [
        { key: "MIN_ADX_TREND", label: "Min ADX Trend", type: "float", group: "strategy", reload_mode: "hot_reload", min: 5, max: 60, step: 0.5, default: 22, unit: "ADX" },
        { key: "RISK_PER_TRADE_PCT", label: "Risk Per Trade (%)", type: "float", group: "risk", reload_mode: "hot_reload", min: 0.1, max: 10, step: 0.1, default: 1.0, unit: "%" },
        { key: "MIN_SL_PCT", label: "Min Stop (%)", type: "float", group: "exit_tsl", reload_mode: "hot_reload", min: 0.5, max: 25, step: 0.5, default: 3.0, unit: "%" },
        { key: "MAX_SL_PCT", label: "Max Stop (%)", type: "float", group: "exit_tsl", reload_mode: "hot_reload", min: 0.5, max: 30, step: 0.5, default: 6.0, unit: "%" },
        { key: "PURE_TSL_MODE", label: "Pure TSL Mode", type: "bool", group: "exit_tsl", reload_mode: "hot_reload", default: false },
        { key: "TRADE_WINDOW_START", label: "Trade Window Start", type: "hhmm", group: "session", reload_mode: "hot_reload", default: "09:00" },
        { key: "TRADE_CUTOFF", label: "Trade Cutoff", type: "hhmm", group: "session", reload_mode: "hot_reload", default: "16:30" },
        { key: "MIN_PRICE_USD", label: "Min Underlying Price", type: "float", group: "runtime", reload_mode: "restart_required", min: 1, max: 500, step: 0.5, default: 5.0, unit: "USD" },
      ],
      allowlisted_screener_metrics: ["adx", "rsi14", "realized_vol", "ema20_slope_5d", "atr_spike_ratio"],
      allowed_gate_operators: ["<", "<=", ">", ">=", "==", "!="],
      allowed_on_missing_modes: ["fail", "pass", "ignore"],
    },
    presets: {
      balanced: { label: "Balanced Baseline", description: "Default thresholds", values: { MIN_ADX_TREND: 22, RISK_PER_TRADE_PCT: 1.0 } },
      conservative: { label: "Conservative", description: "Higher trend selectivity", values: { MIN_ADX_TREND: 26, RISK_PER_TRADE_PCT: 0.7 } },
      momentum_strict: { label: "Strict Momentum", description: "Tight risk + strong ADX", values: { MIN_ADX_TREND: 30, RISK_PER_TRADE_PCT: 0.5 } },
    },
  };
  return Object.assign({}, base, overrides);
}

function createMockEnv({ protocol = "http:", hash = "#monitor", initialData = makeSampleInitialData(), fetchHandler = null } = {}) {
  const fetchCalls = [];
  const sseInstances = [];
  const intervals = [];
  const timeouts = [];
  let nextTimerId = 1;
  let nowMs = 1762862400000; // fixed deterministic clock

  class MockClassList {
    constructor(el) {
      this._el = el;
      this._set = new Set();
    }
    _syncFromStr(str) {
      this._set = new Set(String(str || "").split(/\s+/).filter(Boolean));
    }
    add(...names) {
      names.forEach((n) => n && this._set.add(n));
      this._el._rawClassName = Array.from(this._set).join(" ");
    }
    remove(...names) {
      names.forEach((n) => this._set.delete(n));
      this._el._rawClassName = Array.from(this._set).join(" ");
    }
    contains(name) {
      return this._set.has(name);
    }
    toggle(name, force) {
      if (force === undefined) {
        if (this._set.has(name)) {
          this.remove(name);
          return false;
        }
        this.add(name);
        return true;
      }
      if (force) this.add(name);
      else this.remove(name);
      return !!force;
    }
  }

  class MockNode {
    constructor(doc, nodeType, tagName, ns = null) {
      this._doc = doc;
      this.nodeType = nodeType;
      this.tagName = tagName ? String(tagName).toUpperCase() : "";
      this.namespaceURI = ns;
      this.childNodes = [];
      this.parentNode = null;
      this.attributes = {};
      this.listeners = {};
      this.style = {};
      this.hidden = false;
      this.disabled = false;
      this.checked = false;
      this.value = "";
      this.type = "";
      this._id = "";
      this._rawClassName = "";
      this._text = "";
      this.classList = new MockClassList(this);
      this.dataset = new Proxy({}, {
        set: (target, prop, val) => {
          target[prop] = String(val);
          const kebab = "data-" + String(prop).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
          this.attributes[kebab] = String(val);
          return true;
        },
        get: (target, prop) => target[prop],
      });
    }
    get id() { return this._id; }
    set id(v) {
      if (this._id && this._doc._byId.get(this._id) === this) {
        this._doc._byId.delete(this._id);
      }
      this._id = String(v || "");
      this.attributes.id = this._id;
      if (this._id) this._doc._byId.set(this._id, this);
    }
    get className() { return this._rawClassName; }
    set className(v) {
      this._rawClassName = String(v || "");
      this.classList._syncFromStr(this._rawClassName);
      this.attributes.class = this._rawClassName;
    }
    get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get textContent() {
      if (this.nodeType === 3) return this._text;
      return this.childNodes.map((c) => c.textContent).join("");
    }
    set textContent(v) {
      if (this.nodeType === 3) {
        this._text = String(v ?? "");
        return;
      }
      while (this.childNodes.length) this.removeChild(this.childNodes[0]);
      if (v !== undefined && v !== null && String(v) !== "") {
        this.appendChild(this._doc.createTextNode(String(v)));
      }
    }
    _indexIds(node, add) {
      if (node.nodeType !== 1) return;
      if (node._id) {
        if (add) this._doc._byId.set(node._id, node);
        else if (this._doc._byId.get(node._id) === node) this._doc._byId.delete(node._id);
      }
      node.childNodes.forEach((c) => this._indexIds(c, add));
    }
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = this;
      this.childNodes.push(child);
      this._indexIds(child, true);
      return child;
    }
    removeChild(child) {
      const idx = this.childNodes.indexOf(child);
      if (idx >= 0) {
        this.childNodes.splice(idx, 1);
        child.parentNode = null;
        this._indexIds(child, false);
      }
      return child;
    }
    setAttribute(k, v) {
      const val = String(v);
      this.attributes[k] = val;
      if (k === "id") this.id = val;
      else if (k === "class") this.className = val;
      else if (k === "type") this.type = val;
      else if (k === "disabled") this.disabled = true;
      else if (k === "hidden") this.hidden = true;
      else if (k.startsWith("data-")) {
        const camel = k.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        this.dataset[camel] = val;
      }
    }
    getAttribute(k) {
      if (k === "id") return this._id || null;
      if (k === "disabled") return this.disabled ? "" : null;
      return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
    }
    hasAttribute(k) {
      if (k === "disabled") return !!this.disabled;
      if (k === "hidden") return !!this.hidden;
      return Object.prototype.hasOwnProperty.call(this.attributes, k);
    }
    removeAttribute(k) {
      delete this.attributes[k];
      if (k === "disabled") this.disabled = false;
      if (k === "hidden") this.hidden = false;
    }
    addEventListener(type, fn) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(fn);
    }
    dispatchEvent(ev) {
      if (!ev.target) ev.target = this;
      if (!ev.preventDefault) ev.preventDefault = () => {};
      if (!ev.stopPropagation) ev.stopPropagation = () => {};
      const list = (this.listeners[ev.type] || []).slice();
      for (const fn of list) fn.call(this, ev);
    }
    click() {
      if (this.disabled) return;
      this.dispatchEvent({ type: "click", target: this });
    }
    focus() {
      this._doc.activeElement = this;
    }
    scrollIntoView() {}
    contains(other) {
      let cur = other;
      while (cur) {
        if (cur === this) return true;
        cur = cur.parentNode;
      }
      return false;
    }
    _matchesSingle(sel) {
      let s = sel.trim();
      if (!s) return false;
      const notDisabled = s.includes(":not([disabled])");
      if (notDisabled) {
        if (this.disabled) return false;
        s = s.replace(":not([disabled])", "");
      }
      const notTab = s.includes(':not([tabindex="-1"])');
      if (notTab) {
        if (this.getAttribute("tabindex") === "-1") return false;
        s = s.replace(':not([tabindex="-1"])', "");
      }
      const attrMatches = [];
      s = s.replace(/\[([^\]]+)\]/g, (_, inner) => {
        attrMatches.push(inner);
        return "";
      });
      const parts = s.split(/(?=[.#])/);
      for (const p of parts) {
        if (!p) continue;
        if (p.startsWith(".")) {
          if (!this.classList.contains(p.slice(1))) return false;
        } else if (p.startsWith("#")) {
          if (this._id !== p.slice(1)) return false;
        } else if (this.tagName !== p.toUpperCase()) {
          return false;
        }
      }
      for (const attrExpr of attrMatches) {
        const eqIdx = attrExpr.indexOf("=");
        if (eqIdx === -1) {
          if (!this.hasAttribute(attrExpr.trim())) return false;
        } else {
          const k = attrExpr.slice(0, eqIdx).trim();
          const v = attrExpr.slice(eqIdx + 1).trim().replace(/^["']|["']$/g, "");
          if (this.getAttribute(k) !== v) return false;
        }
      }
      return true;
    }
    matches(selector) {
      return selector.split(",").some((part) => this._matchesSingle(part));
    }
    querySelectorAll(selector) {
      const out = [];
      const walk = (n) => {
        for (const c of n.children) {
          if (c.matches(selector)) out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    }
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] || null;
    }
  }

  const doc = {
    _byId: new Map(),
    readyState: "complete",
    activeElement: null,
    listeners: {},
    createElement(tag) { return new MockNode(doc, 1, tag); },
    createElementNS(ns, tag) { return new MockNode(doc, 1, tag, ns); },
    createTextNode(text) {
      const n = new MockNode(doc, 3, "");
      n._text = String(text ?? "");
      return n;
    },
    getElementById(id) { return doc._byId.get(id) || null; },
    querySelector(sel) { return doc.body.querySelector(sel); },
    querySelectorAll(sel) { return doc.body.querySelectorAll(sel); },
    addEventListener(type, fn) {
      if (!doc.listeners[type]) doc.listeners[type] = [];
      doc.listeners[type].push(fn);
    },
  };
  doc.body = new MockNode(doc, 1, "body");
  const appRoot = doc.createElement("div");
  appRoot.id = "workbench-app";
  doc.body.appendChild(appRoot);

  const storageMap = new Map();
  const sessionStorage = {
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => { storageMap.set(k, String(v)); },
    removeItem: (k) => { storageMap.delete(k); },
  };

  class MockEventSource {
    constructor(url) {
      this.url = url;
      this.closed = false;
      this._listeners = {};
      sseInstances.push(this);
    }
    addEventListener(type, fn) {
      if (!this._listeners[type]) this._listeners[type] = [];
      this._listeners[type].push(fn);
    }
    emit(type, payload) {
      const list = this._listeners[type] || [];
      const dataStr = typeof payload === "string" ? payload : JSON.stringify(payload);
      list.forEach((fn) => fn({ type, data: dataStr }));
    }
    triggerOpen() {
      if (this.onopen) this.onopen({ type: "open" });
    }
    triggerError() {
      if (this.onerror) this.onerror({ type: "error" });
    }
    close() {
      this.closed = true;
    }
  }

  const winListeners = {};
  const location = { protocol, hash };
  const win = {
    location,
    sessionStorage,
    EventSource: MockEventSource,
    INITIAL_DATA: initialData,
    addEventListener(type, fn) {
      if (!winListeners[type]) winListeners[type] = [];
      winListeners[type].push(fn);
    },
    dispatchEvent(ev) {
      (winListeners[ev.type] || []).forEach((fn) => fn(ev));
    },
    setHash(newHash) {
      location.hash = newHash;
      win.dispatchEvent({ type: "hashchange" });
    },
  };
  win.window = win;

  async function mockFetch(url, opts = {}) {
    fetchCalls.push({ url, opts });
    if (fetchHandler) {
      const res = await fetchHandler(url, opts);
      return {
        ok: res.status >= 200 && res.status < 300,
        status: res.status,
        json: async () => res.body || {},
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }

  const fakeSetInterval = (fn, ms) => {
    const id = nextTimerId++;
    intervals.push({ id, fn, ms, cleared: false });
    return id;
  };
  const fakeClearInterval = (id) => {
    const item = intervals.find((t) => t.id === id);
    if (item) item.cleared = true;
  };
  const fakeSetTimeout = (fn, ms) => {
    const id = nextTimerId++;
    timeouts.push({ id, fn, ms, cleared: false, ran: false });
    return id;
  };
  const fakeClearTimeout = (id) => {
    const item = timeouts.find((t) => t.id === id);
    if (item) item.cleared = true;
  };

  const sandbox = {
    window: win,
    document: doc,
    sessionStorage,
    EventSource: MockEventSource,
    fetch: mockFetch,
    INITIAL_DATA: initialData,
    setInterval: fakeSetInterval,
    clearInterval: fakeClearInterval,
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClearTimeout,
    Date: Object.assign(function (...args) { return args.length ? new Date(...args) : new Date(nowMs); }, {
      now: () => nowMs,
      parse: Date.parse,
      UTC: Date.UTC,
    }),
    URLSearchParams,
    console,
    Math,
    Set,
    Map,
    Array,
    Object,
    String,
    Number,
    Boolean,
    JSON,
    Promise,
  };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(workbenchCode, ctx, { filename: "workbench.js" });

  return {
    api: win.QuantWorkbench,
    win,
    doc,
    fetchCalls,
    sseInstances,
    intervals,
    timeouts,
    advanceTime: (deltaMs) => { nowMs += deltaMs; },
    flushMicrotasks: () => new Promise((r) => setImmediate(r)),
  };
}

async function runAll() {
  // 1. file:// offline read-only viewer
  {
    const env = createMockEnv({ protocol: "file:" });
    assert.strictEqual(env.fetchCalls.length, 0, "file:// must never call fetch()");
    assert.strictEqual(env.sseInstances.length, 0, "file:// must never open EventSource");
    assert.strictEqual(env.intervals.some((t) => t.ms === 3500), false, "Must not use 3500ms interval");
    const ro = env.doc.getElementById("readonly-banner");
    assert.strictEqual(ro.hidden, false);
    assert.ok(ro.textContent.includes("Read-Only Offline Viewer"));
    assert.strictEqual(env.doc.getElementById("panic-flatten-btn").disabled, true);
    env.win.setHash("#config");
    assert.strictEqual(env.doc.getElementById("add-param-btn").disabled, true);
    assert.strictEqual(env.doc.getElementById("load-profile-btn").disabled, true);
    assert.strictEqual(env.doc.getElementById("cfg-input-MIN_ADX_TREND").disabled, true);
    env.api.destroyWorkbench();
  }

  // 2. controller_attached === false detached read-only server
  {
    const env = createMockEnv({
      initialData: makeSampleInitialData({
        controller_attached: false,
        read_only_reason: "Detached viewer: no Orchestrator running.",
      }),
    });
    const ro = env.doc.getElementById("readonly-banner");
    assert.strictEqual(ro.hidden, false);
    assert.ok(ro.textContent.includes("Detached Read-Only Server"));
    assert.ok(ro.textContent.includes("Detached viewer: no Orchestrator running."));
    assert.strictEqual(env.doc.getElementById("panic-flatten-btn").disabled, true);
    assert.ok(env.doc.getElementById("signals-grid").textContent.includes("3LNV"));
    env.api.destroyWorkbench();
  }

  // 3. Dynamic telemetry, quarantine & halted banners, legacy cleanup, and SVG equity chart
  {
    const env = createMockEnv({
      initialData: makeSampleInitialData({
        system_halted: true,
        halt_reason: "Panic flatten triggered",
        pending_restart_keys: ["MIN_PRICE_USD"],
        config_quarantine_warnings: ["Unknown key LEGACY_FOO quarantined"],
      }),
    });
    const bodyText = env.doc.body.textContent;
    assert.ok(!bodyText.includes("Connors 3-RSI"));
    assert.ok(!bodyText.includes("84% EUR"));
    assert.ok(!bodyText.includes("120 Assets"));

    const qBanner = env.doc.getElementById("quarantine-banner");
    const hBanner = env.doc.getElementById("halted-banner");
    assert.strictEqual(qBanner.hidden, false);
    assert.ok(qBanner.textContent.includes("LEGACY_FOO"));
    assert.strictEqual(hBanner.hidden, false);
    assert.ok(hBanner.textContent.includes("SYSTEM HALTED"));
    assert.ok(env.doc.getElementById("header-pending-restart-pill").textContent.includes("MIN_PRICE_USD"));
    assert.ok(env.doc.getElementById("header-rate-telemetry").textContent.includes("waits 2"));
    assert.ok(env.doc.getElementById("header-rate-telemetry").textContent.includes("429s 1"));
    assert.strictEqual(env.doc.getElementById("resume-trading-btn").disabled, false);

    // SVG Equity chart positive & negative gradient switch
    env.api.renderEquityChart(
      [
        { equity: 10000, timestamp: "T1" },
        { equity: 9400, timestamp: "T2" },
      ],
      9400
    );
    assert.strictEqual(env.doc.getElementById("equity-val-current").textContent, "€9,400.00");
    const paths = env.doc.getElementById("equity-chart-group").querySelectorAll("path");
    assert.ok(paths.length >= 2, "Equity chart must emit area and line SVG paths");
    assert.ok(paths[0].getAttribute("fill").includes("equity-gradient-rose"));
    env.api.destroyWorkbench();
  }

  // 4. Hash routing, funnel deep-link + cross-sectional callout, and structural fallback notice
  {
    const env = createMockEnv();
    env.win.setHash("#config?param=MIN_ADX_TREND&gate=adx_trend_gate");
    assert.strictEqual(env.doc.getElementById("tab-config").getAttribute("aria-selected"), "true");
    assert.strictEqual(env.doc.getElementById("workspace-config").hidden, false);
    assert.strictEqual(env.doc.getElementById("workspace-monitor").hidden, true);
    const callout = env.doc.getElementById("config-deeplink-callout");
    assert.ok(callout && callout.textContent.includes("median 23.80"));
    assert.strictEqual(env.doc.activeElement && env.doc.activeElement.id, "cfg-input-MIN_ADX_TREND");

    env.win.setHash("#config?param=DELETED_KEY&gate=stale_epoch_superseded");
    const fallback = env.doc.getElementById("config-deeplink-fallback");
    assert.ok(fallback && fallback.textContent.includes("structural"));

    env.win.setHash("#audit");
    assert.strictEqual(env.doc.getElementById("tab-audit").getAttribute("aria-selected"), "true");
    assert.ok(env.doc.getElementById("audit-status-card").textContent.includes("TREND"));
    env.api.destroyWorkbench();
  }

  // 5. Locked Diff Drawer skips /api/config/preview + catches local cross-field errors; Unlocked runs preview
  {
    const previewCalls = [];
    const env = createMockEnv({
      hash: "#config",
      fetchHandler: async (url, opts) => {
        if (url === "/api/config/preview") {
          previewCalls.push(JSON.parse(opts.body));
          return {
            status: 200,
            body: {
              valid: true,
              errors: [],
              non_simulatable_keys: [],
              phase2_impact: { newly_blocked: [], newly_passed: [], net_delta: 0 },
            },
          };
        }
        return { status: 200, body: {} };
      },
    });
    env.api.stageParameterValue("MIN_ADX_TREND", 28);
    // Flush any queued timeouts
    for (const t of env.timeouts) {
      if (!t.cleared && !t.ran) { t.ran = true; await t.fn(); }
    }
    assert.strictEqual(previewCalls.length, 0, "Locked mode must NEVER hit /api/config/preview");
    assert.ok(env.doc.getElementById("diff-locked-hint"));

    // Cross-field local invariant error: MIN_SL_PCT (10) >= MAX_SL_PCT (6)
    env.api.stageParameterValue("MIN_SL_PCT", 10);
    const localDiff = env.api.computeLocalValidationAndDiff(
      env.api.getState().data.config.values,
      env.api.getState().stagedChanges,
      env.api.getState().data.schema.parameters
    );
    assert.ok(localDiff.errors.length > 0);
    assert.ok(localDiff.errors.some((e) => e.message.includes("MIN_SL_PCT")));
    assert.strictEqual(env.doc.getElementById("commit-config-btn").disabled, true);

    // Fix invalid value, unlock session, and verify /api/config/preview fires
    env.api.stageParameterValue("MIN_SL_PCT", 2.5);
    env.win.sessionStorage.setItem(env.api.TOKEN_STORAGE_KEY, "tok-123");
    env.api.stageParameterValue("MIN_ADX_TREND", 29);
    for (const t of env.timeouts) {
      if (!t.cleared && !t.ran) { t.ran = true; await t.fn(); }
    }
    await env.flushMicrotasks();
    assert.strictEqual(previewCalls.length, 1, "Unlocked mode must hit /api/config/preview");
    assert.strictEqual(previewCalls[0].expected_epoch_id, "epoch-1");
    env.api.destroyWorkbench();
  }

  // 6. AE1: 401 Auth Modal + Escape preserves stagedChanges + valid unlock retries mutation
  {
    let commitAttempts = 0;
    const env = createMockEnv({
      hash: "#config",
      fetchHandler: async (url, opts) => {
        const hdrs = opts.headers || {};
        if (url === "/api/config") {
          commitAttempts += 1;
          if (hdrs["X-Dashboard-Token"] !== "secret-99") {
            return { status: 401, body: { error: "Token required" } };
          }
          const body = JSON.parse(opts.body);
          assert.strictEqual(body.expected_epoch_id, "epoch-1");
          assert.strictEqual(body.patch.MIN_ADX_TREND, 31);
          return {
            status: 200,
            body: {
              config_epoch_id: "epoch-2",
              snapshot: makeSampleInitialData({
                config_epoch_id: "epoch-2",
                config: { config_epoch_id: "epoch-2", values: { MIN_ADX_TREND: 31 }, dynamic_parameters: {} },
              }).config,
            },
          };
        }
        if (url === "/api/auth/verify") {
          if (hdrs["X-Dashboard-Token"] === "secret-99") {
            return { status: 200, body: { ok: true } };
          }
          return { status: 401, body: { error: "Bad token" } };
        }
        return { status: 200, body: {} };
      },
    });
    env.api.stageParameterValue("MIN_ADX_TREND", 31);
    await env.api.submitConfigCommit("Raise trend floor");
    assert.strictEqual(commitAttempts, 1);
    const authDialog = env.doc.getElementById("auth-modal");
    assert.ok(authDialog, "401 must open #auth-modal");
    assert.strictEqual(authDialog.getAttribute("role"), "dialog");
    assert.strictEqual(authDialog.getAttribute("aria-modal"), "true");

    // Cancel via Escape: stagedChanges MUST survive
    authDialog.dispatchEvent({ type: "keydown", key: "Escape", preventDefault() {} });
    assert.strictEqual(env.doc.getElementById("auth-modal"), null);
    assert.strictEqual(env.api.getState().stagedChanges.MIN_ADX_TREND, 31);
    assert.strictEqual(env.api.getState().draftBaseEpochId, "epoch-1");

    // Trigger commit again, submit valid token -> auto-retries commit!
    await env.api.submitConfigCommit("Raise trend floor");
    assert.strictEqual(commitAttempts, 2);
    env.doc.getElementById("auth-token-input").value = "secret-99";
    env.doc.getElementById("auth-submit-btn").click();
    await env.flushMicrotasks();
    await env.flushMicrotasks();

    assert.strictEqual(commitAttempts, 3, "Valid unlock must auto-retry blocked mutation");
    assert.strictEqual(env.win.sessionStorage.getItem(env.api.TOKEN_STORAGE_KEY), "secret-99");
    assert.strictEqual(Object.keys(env.api.getState().stagedChanges).length, 0);
    assert.strictEqual(env.api.getState().data.config_epoch_id, "epoch-2");
    env.api.destroyWorkbench();
  }

  // 7. AE2: SSE remote epoch banner & 409 Conflict Rebase (Keep Mine / Use Remote)
  {
    const env = createMockEnv({
      hash: "#config",
      fetchHandler: async (url) => {
        if (url.startsWith("/api/data")) {
          return {
            status: 200,
            body: makeSampleInitialData({ config_epoch_id: "epoch-2" }),
          };
        }
        if (url === "/api/config") {
          return {
            status: 409,
            body: {
              error: "Epoch mismatch",
              active_epoch_id: "epoch-3",
              config: {
                config_epoch_id: "epoch-3",
                values: { MIN_ADX_TREND: 26, RISK_PER_TRADE_PCT: 0.8 },
              },
            },
          };
        }
        return { status: 200, body: {} };
      },
    });
    env.win.sessionStorage.setItem(env.api.TOKEN_STORAGE_KEY, "tok");
    env.api.stageParameterValue("MIN_ADX_TREND", 30);
    env.api.stageParameterValue("RISK_PER_TRADE_PCT", 1.5);

    // Remote epoch changes via SSE without wiping draft
    env.sseInstances[0].emit("config_epoch_changed", { config_epoch_id: "epoch-2" });
    assert.ok(env.doc.getElementById("remote-epoch-changed-banner"));
    assert.strictEqual(env.api.getState().stagedChanges.MIN_ADX_TREND, 30);

    // Commit hits 409 conflict
    await env.api.submitConfigCommit("test conflict");
    assert.ok(env.doc.getElementById("conflict-rebase-panel"), "409 must open conflict rebase panel");

    env.api.applyRebaseChoices({ MIN_ADX_TREND: "mine", RISK_PER_TRADE_PCT: "remote" });
    assert.strictEqual(env.api.getState().draftBaseEpochId, "epoch-3");
    assert.strictEqual(env.api.getState().stagedChanges.MIN_ADX_TREND, 30);
    assert.strictEqual(Object.prototype.hasOwnProperty.call(env.api.getState().stagedChanges, "RISK_PER_TRADE_PCT"), false);
    env.api.destroyWorkbench();
  }

  // 8. Profiles, Dynamic Param Add/Delete (preserving stagedChanges on 401 retry), Panic/Resume & Re-Epoch modals
  {
    let deleteAttempts = 0;
    let resumeBody = null;
    let reEpochBody = null;
    const env = createMockEnv({
      hash: "#config",
      initialData: makeSampleInitialData({
        system_halted: true,
        active_signals: [
          {
            ticker: "3LNV",
            direction: "LONG",
            shares: 10,
            entry_price: 40,
            current_price: 42,
            stop_price: 38,
            config_epoch_id: "epoch-0",
            status: "PANIC_FAILED",
          },
        ],
        config: {
          config_epoch_id: "epoch-1",
          values: { MIN_ADX_TREND: 22, RISK_PER_TRADE_PCT: 1.0, HARD_STOP_PCT: 3.0, TSL_INITIAL_STOP_PCT: 6.0, PURE_TSL_MODE: false, CUSTOM_GATE: 35 },
          dynamic_parameters: {
            CUSTOM_GATE: {
              key: "CUSTOM_GATE",
              label: "Custom Gate",
              type: "float",
              group: "screener",
              hot_reload: true,
              default: 35,
              rule: { kind: "screener_metric_gate", metric: "rsi14", operator: ">=", threshold_param: "CUSTOM_GATE", on_missing: "fail" },
            },
          },
        },
        schema: {
          parameters: [
            { key: "MIN_ADX_TREND", label: "Min ADX Trend", type: "float", group: "strategy", hot_reload: true, min: 5, max: 60, step: 0.5, default: 22 },
            { key: "CUSTOM_GATE", label: "Custom Gate", type: "float", group: "screener", hot_reload: true, default: 35, dynamic: true, rule: { kind: "screener_metric_gate", metric: "rsi14", operator: ">=", threshold_param: "CUSTOM_GATE", on_missing: "fail" } },
          ],
          allowlisted_screener_metrics: ["adx", "rsi14"],
          allowed_gate_operators: [">=", "<="],
          allowed_on_missing_modes: ["fail", "pass"],
        },
      }),
      fetchHandler: async (url, opts) => {
        const hdrs = opts.headers || {};
        if (url === "/api/params/CUSTOM_GATE/delete") {
          deleteAttempts += 1;
          if (hdrs["X-Dashboard-Token"] !== "tok-ok") return { status: 401, body: { error: "Locked" } };
          return { status: 200, body: { ok: true, config_epoch_id: "epoch-2", snapshot: { config_epoch_id: "epoch-2", values: { MIN_ADX_TREND: 22 }, dynamic_parameters: {} } } };
        }
        if (url === "/api/auth/verify") {
          return { status: 200, body: { ok: true } };
        }
        if (url === "/api/resume") {
          resumeBody = JSON.parse(opts.body);
          return { status: 200, body: { ok: true } };
        }
        if (url === "/api/positions/3LNV/re-epoch/preview") {
          return {
            status: 200,
            body: {
              ticker: "3LNV",
              current_epoch_id: "epoch-0",
              target_epoch_id: "epoch-1",
              action: "keep_existing_stop",
              requires_tsl_confirmation: true,
              old_stop: 38.0,
              new_stop: 38.0,
            },
          };
        }
        if (url === "/api/positions/3LNV/re-epoch") {
          reEpochBody = JSON.parse(opts.body);
          return { status: 200, body: { ok: true } };
        }
        return { status: 200, body: {} };
      },
    });

    // Profile loader stages into draft and prompts on dirty second profile
    env.api.loadBuiltinProfileIntoDraft("conservative");
    assert.strictEqual(env.api.getState().stagedChanges.MIN_ADX_TREND, 26);
    env.api.loadBuiltinProfileIntoDraft("momentum_strict");
    assert.ok(env.doc.getElementById("profile-confirm-modal"));
    env.doc.getElementById("profile-confirm-modal").dispatchEvent({ type: "keydown", key: "Escape", preventDefault() {} });

    // Immediate Delete param hits 401 -> Auth Modal -> retry WITHOUT wiping stagedChanges!
    env.api.openDeleteParamModal("CUSTOM_GATE");
    env.doc.getElementById("delete-param-confirm-btn").click();
    await env.flushMicrotasks();
    assert.ok(env.doc.getElementById("auth-modal"));
    env.doc.getElementById("auth-token-input").value = "tok-ok";
    env.doc.getElementById("auth-submit-btn").click();
    await env.flushMicrotasks();
    await env.flushMicrotasks();
    assert.strictEqual(deleteAttempts, 2);
    assert.strictEqual(env.api.getState().stagedChanges.MIN_ADX_TREND, 26, "Delete retry must preserve stagedChanges");

    // Resume modal unprotected gate
    env.api.openResumeModal();
    assert.strictEqual(env.doc.getElementById("resume-confirm-btn").disabled, true);
    const cb = env.doc.getElementById("resume-confirm-unprotected");
    cb.checked = true;
    cb.dispatchEvent({ type: "change", target: cb });
    assert.strictEqual(env.doc.getElementById("resume-confirm-btn").disabled, false);
    env.doc.getElementById("resume-confirm-btn").click();
    await env.flushMicrotasks();
    assert.deepStrictEqual(resumeBody, { confirm_unprotected: true });

    // Re-epoch modal requires TSL mode change checkbox when server flags requires_tsl_confirmation
    await env.api.openReEpochModal("3LNV");
    assert.strictEqual(env.doc.getElementById("re-epoch-confirm-btn").disabled, true);
    const tslCb = env.doc.getElementById("re-epoch-tsl-confirm");
    tslCb.checked = true;
    tslCb.dispatchEvent({ type: "change", target: tslCb });
    assert.strictEqual(env.doc.getElementById("re-epoch-confirm-btn").disabled, false);
    env.doc.getElementById("re-epoch-confirm-btn").click();
    await env.flushMicrotasks();
    assert.strictEqual(reEpochBody.confirm_pure_tsl_mode_change, true);
    env.api.destroyWorkbench();
  }

  // 9. SSE reconnect catch-up, >25s Heartbeat Stale indicator, and focus/modal preservation
  {
    let catchUpCalls = 0;
    const env = createMockEnv({
      hash: "#config",
      fetchHandler: async (url) => {
        if (url.startsWith("/api/data")) {
          catchUpCalls += 1;
          return { status: 200, body: makeSampleInitialData({ current_equity: 10500 }) };
        }
        return { status: 200, body: {} };
      },
    });
    assert.strictEqual(env.sseInstances.length, 1);
    env.sseInstances[0].triggerOpen();

    // Focus input on #config and ensure SSE snapshot preserves focus AND open modal
    const inputEl = env.doc.getElementById("cfg-input-MIN_ADX_TREND");
    inputEl.focus();
    assert.strictEqual(env.doc.activeElement, inputEl);
    env.sseInstances[0].emit("snapshot", makeSampleInitialData({ current_equity: 10420 }));
    assert.strictEqual(env.doc.activeElement, inputEl, "SSE snapshot must NOT clobber focused input");
    env.api.openAuthModal();
    assert.ok(env.doc.getElementById("auth-modal"));
    env.sseInstances[0].emit("snapshot", makeSampleInitialData({ current_equity: 10440 }));
    assert.ok(env.doc.getElementById("auth-modal"), "SSE snapshot must NOT wipe open modal");
    env.doc.getElementById("auth-modal").dispatchEvent({ type: "keydown", key: "Escape", preventDefault() {} });

    // Advance >25s without heartbeat -> Heartbeat Stale
    env.advanceTime(26000);
    const uiInterval = env.intervals.find((i) => i.ms === 1000 && !i.cleared);
    assert.ok(uiInterval);
    uiInterval.fn();
    assert.ok(env.doc.getElementById("header-sse-health").textContent.includes("Heartbeat Stale"));

    // Heartbeat restores health
    env.sseInstances[0].emit("heartbeat", {});
    uiInterval.fn();
    assert.ok(env.doc.getElementById("header-sse-health").textContent.includes("Live"));

    // Disconnect -> exponential backoff reconnect -> onopen triggers one-shot GET /api/data catch-up
    env.sseInstances[0].triggerError();
    const reconnectTimer = env.timeouts.find((t) => !t.cleared && t.ms >= 1000 && t.ms <= 20000);
    assert.ok(reconnectTimer, "SSE error must schedule exponential backoff reconnect");
    reconnectTimer.fn();
    assert.strictEqual(env.sseInstances.length, 2);
    env.sseInstances[1].triggerOpen();
    await env.flushMicrotasks();
    assert.strictEqual(catchUpCalls, 1, "SSE reconnect open must trigger one-shot /api/data catch-up");
    env.api.destroyWorkbench();
  }

  // 10. Regression: #config-search-input & #config-group-select keep toolbar DOM node identity, focus, and cursor while filtering
  {
    const env = createMockEnv({ hash: "#config" });
    const searchBefore = env.doc.getElementById("config-search-input");
    const groupBefore = env.doc.getElementById("config-group-select");
    assert.ok(searchBefore);
    assert.ok(env.doc.getElementById("cfg-input-MIN_PRICE_USD"), "MIN_PRICE_USD visible before filtering");

    searchBefore.focus();
    searchBefore.value = "ADX";
    searchBefore.selectionStart = 3;
    searchBefore.selectionEnd = 3;
    searchBefore.dispatchEvent({ type: "input", target: searchBefore });

    assert.strictEqual(env.doc.getElementById("config-search-input"), searchBefore, "#config-search-input node must stay mounted during typing");
    assert.strictEqual(env.doc.activeElement, searchBefore, "#config-search-input focus must survive filtering");
    assert.strictEqual(searchBefore.selectionStart, 3);
    assert.strictEqual(searchBefore.selectionEnd, 3);
    assert.ok(env.doc.getElementById("cfg-input-MIN_ADX_TREND"), "Matched param must stay rendered");
    assert.strictEqual(env.doc.getElementById("cfg-input-MIN_PRICE_USD"), null, "Unmatched param must be filtered out");

    // SSE snapshot arriving while user is focused in #config-search-input must also keep node + focus
    env.sseInstances[0].emit("snapshot", makeSampleInitialData({ current_equity: 10490 }));
    assert.strictEqual(env.doc.getElementById("config-search-input"), searchBefore);
    assert.strictEqual(env.doc.activeElement, searchBefore);

    // Clearing search restores all params without rebuilding toolbar nodes
    searchBefore.value = "";
    searchBefore.dispatchEvent({ type: "input", target: searchBefore });
    assert.ok(env.doc.getElementById("cfg-input-MIN_PRICE_USD"));
    assert.strictEqual(env.doc.getElementById("config-search-input"), searchBefore);

    // Changing #config-group-select filters without rebuilding #config-group-select or #config-search-input
    groupBefore.value = "risk";
    groupBefore.dispatchEvent({ type: "change", target: groupBefore });
    assert.strictEqual(env.doc.getElementById("config-group-select"), groupBefore);
    assert.strictEqual(env.doc.getElementById("config-search-input"), searchBefore);
    assert.ok(env.doc.getElementById("cfg-input-RISK_PER_TRADE_PCT"));
    assert.strictEqual(env.doc.getElementById("cfg-input-MIN_ADX_TREND"), null);

    env.api.destroyWorkbench();
  }

  // 11. Mission Control: Live 7-step stepper and SSE progress updates
  {
    const env = createMockEnv({ hash: "#monitor" });
    const stepper = env.doc.getElementById("workflow-stepper");
    assert.ok(stepper, "Workflow stepper must be rendered on #monitor");
    assert.ok(env.doc.getElementById("step-1"), "Step 1 Market Check rendered");
    assert.ok(env.doc.getElementById("step-7"), "Step 7 Standby rendered");

    // Emit intermediate pipeline_progress SSE event (Step 3: Trend Filter)
    env.sseInstances[0].emit("pipeline_progress", {
      step: 3,
      name: "Trend Filter",
      detail: "Screening universe indicators against directional_trend rules.",
      status: "active",
      cycle_id: "cycle-test-1",
      active_assets: ["QQQ3l_EQ"],
    });

    const step3 = env.doc.getElementById("step-3");
    assert.ok(step3.classList.contains("wb-step--active"), "Step 3 must have active state");
    const step1 = env.doc.getElementById("step-1");
    assert.ok(step1.classList.contains("wb-step--completed"), "Step 1 must have completed state");

    const nowVal = env.doc.getElementById("workflow-now-val");
    assert.ok(nowVal.textContent.includes("Screening universe indicators"), "Now box updates from SSE");

    env.api.destroyWorkbench();
  }

  // 12. Plain-English Rejection Translation and Expandable Stock Tracker
  {
    const env = createMockEnv({ hash: "#monitor" });
    const trackerToggle = env.doc.getElementById("tracker-toggle-btn");
    assert.ok(trackerToggle, "Tracker toggle button must be rendered");

    // Expand stock tracker
    trackerToggle.click();
    assert.ok(env.doc.querySelector(".wb-tracker-grid"), "Tracker grid rendered upon expansion");
    const expandedToggle = env.doc.getElementById("tracker-toggle-btn");
    assert.ok(expandedToggle.textContent.includes("Hide Universe Stock Tracker"), "Button text toggles to Hide");

    // Collapse stock tracker
    expandedToggle.click();
    assert.strictEqual(env.doc.querySelector(".wb-tracker-grid"), null, "Tracker grid unmounted on collapse");

    env.api.destroyWorkbench();
  }

  // 13. Default Simple Mode vs Advanced Mode in #config
  {
    const env = createMockEnv({ hash: "#config" });
    const quickCard = env.doc.getElementById("quick-controls-card");
    assert.ok(quickCard, "Quick controls card rendered by default in Simple Mode");
    assert.strictEqual(quickCard.hidden, false);

    const slot = env.doc.getElementById("config-param-groups-slot");
    assert.strictEqual(slot.hidden, true, "Full 50-param groups slot hidden in Simple Mode");

    const drawer = env.doc.getElementById("diff-drawer");
    assert.strictEqual(drawer.hidden, true, "Diff drawer hidden in Simple Mode");

    // Click Conservative in Quick Controls
    const conBtn = env.doc.getElementById("quick-risk-conservative");
    assert.ok(conBtn, "Conservative risk button exists");
    conBtn.click();

    const state = env.api.getState();
    assert.strictEqual(state.stagedChanges["MAX_RISK_PER_TRADE"], 0.0075);
    assert.strictEqual(state.stagedChanges["ATR_SL_MULTIPLIER"], 2.5);

    // Select VWAP Strategy in Quick Controls
    const stratSelect = env.doc.getElementById("quick-strat-select");
    assert.ok(stratSelect, "Quick strategy select exists");
    stratSelect.value = "vwap";
    stratSelect.dispatchEvent({ type: "change", target: stratSelect });
    assert.strictEqual(state.stagedChanges["ACTIVE_STRATEGY"], "vwap");

    // Switch to Advanced Mode via toggle button
    const toggleAdvBtn = env.doc.getElementById("toggle-advanced-from-quick-btn");
    assert.ok(toggleAdvBtn);
    toggleAdvBtn.click();

    assert.strictEqual(state.viewMode, "advanced");
    assert.strictEqual(slot.hidden, false, "Full groups slot revealed in Advanced Mode");
    assert.strictEqual(drawer.hidden, false, "Diff drawer revealed in Advanced Mode");

    env.api.destroyWorkbench();
  }
}

runAll()
  .then(() => {
    console.log("ALL_NODE_WORKBENCH_TESTS_PASSED");
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
"""


class TestWorkbenchAssetsFunctional(unittest.TestCase):
    """Execute functional JS behavioral tests against workbench.js inside Node."""

    def test_workbench_js_functional_suite(self) -> None:
        node = _find_node()
        env = dict(os.environ)
        env["WORKBENCH_JS_PATH"] = str(WORKBENCH_JS_PATH)
        res = subprocess.run(
            [node, "-e", _NODE_HARNESS_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Node functional harness failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        self.assertIn("ALL_NODE_WORKBENCH_TESTS_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
