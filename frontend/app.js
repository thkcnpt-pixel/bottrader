// VN Stock Swing Bot — Personal PWA Logic (Firebase v10 Modular SDK)
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.5/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from "https://www.gstatic.com/firebasejs/10.12.5/firebase-auth.js";
import {
  arrayUnion,
  collection,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  getFirestore,
  limit,
  orderBy,
  query,
  setDoc,
} from "https://www.gstatic.com/firebasejs/10.12.5/firebase-firestore.js";
import {
  getMessaging,
  getToken,
  isSupported as isMessagingSupported,
  onMessage,
} from "https://www.gstatic.com/firebasejs/10.12.5/firebase-messaging.js";

import { FCM_VAPID_KEY, FIREBASE_CONFIG } from "./firebase-config.js";

// -------------------- Helpers --------------------
const $ = (id) => document.getElementById(id);

function fmtVnd(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  return Math.round(Number(val)).toLocaleString("vi-VN") + " đ";
}

function fmtSignedVnd(val) {
  const n = Math.round(Number(val || 0));
  return `${n >= 0 ? "+" : ""}${n.toLocaleString("vi-VN")} đ`;
}

function fmtNum(val, digits = 2) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
  return Number(val).toFixed(digits);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function currentMonthKey() {
  return new Date().toISOString().slice(0, 7); // YYYY-MM
}

function showToast(msg, durationMs = 4500) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.add("hidden"), durationMs);
}

// -------------------- State --------------------
let db = null;
let auth = null;
let messaging = null;
let swReg = null;
let reportsCache = {};
let latestReport = null;
let portfolioItems = [];
let currentPfFilter = "holding";

const DEFAULT_ACCOUNT = {
  cash_balance: 0,
  total_deposited: 0,
  fee_buy_pct: 0.0,
  fee_sell_pct: 0.0,
  tax_sell_pct: 0.1,
  cash_flows: [],
  realized_trades: [],
  monthly_snapshots: {},
};

let accountState = { ...DEFAULT_ACCOUNT };

// -------------------- Service Worker Helper (Subpath-Safe for GitHub Pages) --------------------
async function ensureActiveServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    throw new Error("Trình duyệt không hỗ trợ Service Worker.");
  }
  const swUrl = new URL("./firebase-messaging-sw.js", window.location.href).href;
  const swScope = new URL("./", window.location.href).pathname;

  const reg = await navigator.serviceWorker.register(swUrl, { scope: swScope });
  if (reg.active) {
    swReg = reg;
    return reg;
  }

  // Wait for installing/waiting SW to become active
  await new Promise((resolve, reject) => {
    const sw = reg.installing || reg.waiting;
    if (!sw) {
      resolve();
      return;
    }
    const timer = setTimeout(() => reject(new Error("Service Worker kích hoạt quá lâu.")), 10000);
    sw.addEventListener("statechange", () => {
      if (sw.state === "activated") {
        clearTimeout(timer);
        resolve();
      } else if (sw.state === "redundant") {
        clearTimeout(timer);
        reject(new Error("Service Worker bị hủy kích hoạt."));
      }
    });
  });

  swReg = reg;
  return reg;
}

// -------------------- Bootstrap --------------------
async function init() {
  if (!FIREBASE_CONFIG.apiKey || FIREBASE_CONFIG.apiKey.startsWith("REPLACE_")) {
    $("config-warning").classList.remove("hidden");
    return;
  }

  const app = initializeApp(FIREBASE_CONFIG);
  auth = getAuth(app);
  db = getFirestore(app);

  // Bind login & UI events IMMEDIATELY before any async operations
  bindEvents();

  // Only initialize Web ServiceWorker / Web Push when running in a browser (NOT inside Native APK)
  if (!getNativePushPlugin()) {
    if ("serviceWorker" in navigator) {
      ensureActiveServiceWorker().catch((err) => console.warn("SW preload warning:", err));
    }
    isMessagingSupported()
      .then((supported) => {
        if (!supported) return;
        messaging = getMessaging(app);
        onMessage(messaging, (payload) => {
          const title = payload.notification?.title || "SwingBot";
          const body = payload.notification?.body || "";
          showToast(`${title}: ${body}`, 6000);
        });
      })
      .catch((err) => console.warn("FCM messaging unavailable:", err));
  }

  onAuthStateChanged(auth, async (user) => {
    if (user) {
      $("auth-view").classList.add("hidden");
      $("app-view").classList.remove("hidden");
      $("bottom-nav").classList.remove("hidden");
      $("user-badge").classList.remove("hidden");
      $("user-badge").classList.add("flex");
      $("user-email-short").textContent = user.email;
      $("settings-email").textContent = user.email;
      await refreshAllData();
    } else {
      $("auth-view").classList.remove("hidden");
      $("app-view").classList.add("hidden");
      $("bottom-nav").classList.add("hidden");
      $("user-badge").classList.add("hidden");
    }
  });
}

// -------------------- Event Binding --------------------
function bindEvents() {
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("login-email").value.trim();
    const pass = $("login-password").value;
    $("login-error").classList.add("hidden");
    $("btn-login").disabled = true;
    $("btn-login").textContent = "Đang đăng nhập...";
    try {
      await signInWithEmailAndPassword(auth, email, pass);
    } catch (err) {
      $("login-error").textContent = "Đăng nhập thất bại: " + (err.message || err.code);
      $("login-error").classList.remove("hidden");
    } finally {
      $("btn-login").disabled = false;
      $("btn-login").textContent = "Đăng nhập";
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    await signOut(auth);
  });

  $("btn-refresh").addEventListener("click", async () => {
    showToast("Đang đồng bộ dữ liệu từ Firestore...", 2000);
    await refreshAllData();
  });

  $("btn-live-price")?.addEventListener("click", handleFetchInstantPrices);
  $("btn-rescan-candidates")?.addEventListener("click", handleTriggerRescanCandidates);
  $("gh-token-form")?.addEventListener("submit", handleSaveGhToken);
  loadGhTokenStatus();

  // Bottom tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      ["dashboard", "portfolio", "settings"].forEach((t) => {
        $(`tab-${t}`).classList.toggle("hidden", t !== target);
      });
    });
  });

  $("report-date-select").addEventListener("change", (e) => {
    const dateKey = e.target.value;
    if (dateKey && reportsCache[dateKey]) {
      renderDailyReport(reportsCache[dateKey]);
    }
  });

  // Portfolio filters
  document.querySelectorAll(".pf-filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      currentPfFilter = btn.dataset.pfFilter;
      document.querySelectorAll(".pf-filter-btn").forEach((b) => {
        const active = b.dataset.pfFilter === currentPfFilter;
        b.className = active
          ? "pf-filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600/20 text-emerald-300 border border-emerald-500/40"
          : "pf-filter-btn px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-800/80 text-slate-400 border border-slate-700";
      });
      renderPortfolio();
    });
  });

  // Add/Edit Portfolio Modal
  $("btn-open-add-modal").addEventListener("click", () => openPortfolioModal(null));
  $("btn-close-modal").addEventListener("click", closePortfolioModal);
  $("portfolio-modal").addEventListener("click", (e) => {
    if (e.target === $("portfolio-modal")) closePortfolioModal();
  });
  $("portfolio-form").addEventListener("submit", handleSavePortfolio);

  // Deposit / Withdraw Modal
  $("btn-open-cashflow").addEventListener("click", openCashflowModal);
  $("btn-close-cashflow").addEventListener("click", () => $("cashflow-modal").classList.add("hidden"));
  $("cashflow-modal").addEventListener("click", (e) => {
    if (e.target === $("cashflow-modal")) $("cashflow-modal").classList.add("hidden");
  });
  $("cashflow-form").addEventListener("submit", handleSaveCashflow);

  // Sync Cash Modal
  $("btn-open-sync-cash").addEventListener("click", openSyncCashModal);
  $("btn-close-sync-cash").addEventListener("click", () => $("sync-cash-modal").classList.add("hidden"));
  $("sync-cash-modal").addEventListener("click", (e) => {
    if (e.target === $("sync-cash-modal")) $("sync-cash-modal").classList.add("hidden");
  });
  $("sync-cash-balance")?.addEventListener("input", updateSyncCashPreview);
  $("btn-auto-calc-deposited")?.addEventListener("click", handleAutoCalcDeposited);
  $("sync-cash-form").addEventListener("submit", handleSaveSyncCash);

  // Sell / Take-Profit Modal
  $("btn-close-sell").addEventListener("click", () => $("sell-modal").classList.add("hidden"));
  $("sell-modal").addEventListener("click", (e) => {
    if (e.target === $("sell-modal")) $("sell-modal").classList.add("hidden");
  });
  $("sell-price").addEventListener("input", updateSellPreview);
  $("sell-qty").addEventListener("input", updateSellPreview);
  $("sell-form").addEventListener("submit", handleConfirmSell);

  // Fee settings form
  $("fee-settings-form").addEventListener("submit", handleSaveFeeSettings);

  // FCM Push Notification registration
  $("btn-enable-fcm").addEventListener("click", handleRegisterFcmToken);
}

// -------------------- Data Loading --------------------
async function refreshAllData() {
  await Promise.all([loadDailyReports(), loadAccount(), loadPortfolio(), loadFcmSettings()]);
  renderAccountSummary();
  renderPortfolio();
  if (latestReport) {
    renderDailyReport(latestReport);
  }
}

async function loadAccount() {
  try {
    const snap = await getDoc(doc(db, "settings", "account"));
    if (snap.exists()) {
      accountState = { ...DEFAULT_ACCOUNT, ...snap.data() };
    } else {
      accountState = { ...DEFAULT_ACCOUNT };
    }
    $("set-fee-buy").value = accountState.fee_buy_pct ?? 0;
    $("set-fee-sell").value = accountState.fee_sell_pct ?? 0;
    $("set-tax-sell").value = accountState.tax_sell_pct ?? 0.1;
  } catch (err) {
    console.error("Failed to load settings/account:", err);
  }
}

async function persistAccount(partialUpdates = {}) {
  accountState = {
    ...accountState,
    ...partialUpdates,
    updated_at: new Date().toISOString(),
  };
  await setDoc(doc(db, "settings", "account"), accountState, { merge: true });
}

// -------------------- Daily Reports --------------------
async function loadDailyReports() {
  const select = $("report-date-select");
  try {
    const q = query(collection(db, "daily_reports"), orderBy("date", "desc"), limit(20));
    const snap = await getDocs(q);
    reportsCache = {};
    const dates = [];
    snap.forEach((docSnap) => {
      const data = docSnap.data();
      const key = data.date || docSnap.id;
      reportsCache[key] = data;
      dates.push(key);
    });

    if (dates.length === 0) {
      latestReport = null;
      select.innerHTML = `<option value="">Chưa có báo cáo</option>`;
      renderEmptyReport();
      return;
    }

    latestReport = reportsCache[dates[0]];
    select.innerHTML = dates
      .map((d) => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`)
      .join("");
    renderDailyReport(latestReport);
  } catch (err) {
    console.error("Failed to load daily_reports:", err);
    select.innerHTML = `<option value="">Lỗi tải dữ liệu</option>`;
  }
}

function renderEmptyReport() {
  $("vni-close").textContent = "--";
  $("vni-change").textContent = "--";
  $("vni-ma50-badge").textContent = "Chưa có dữ liệu";
  $("buy-candidates-list").innerHTML = `
    <div class="glass-card rounded-2xl p-6 text-center text-xs text-slate-400">
      Chưa có báo cáo nào trong Firestore <code>daily_reports</code>. Hãy chạy workflow <code>daily_scan.yml</code>.
    </div>`;
}

function renderDailyReport(report) {
  const vni = report.vnindex || {};
  const closeVal = vni.close ?? 0;
  const changePct = vni.change_pct ?? 0;
  const aboveMa50 = vni.above_ma50 !== false;
  const warningFlag = Boolean(vni.warning_flag);

  $("vnindex-warning-banner").classList.toggle("hidden", !warningFlag);

  $("vni-close").textContent = fmtNum(closeVal, 2);
  $("vni-change").textContent = `${changePct >= 0 ? "+" : ""}${fmtNum(changePct, 2)}%`;
  $("vni-change").className = `text-sm font-semibold tabular-nums ${
    changePct >= 0 ? "text-emerald-400" : "text-rose-400"
  }`;

  const badge = $("vni-ma50-badge");
  badge.textContent = aboveMa50 ? "✅ Trên MA50" : "⚠️ Dưới MA50";
  badge.className = aboveMa50
    ? "inline-block px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40"
    : "inline-block px-2.5 py-1 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/40";
  $("vni-ma50-val").textContent = `MA50: ${fmtNum(vni.ma50, 2)}`;

  const alerts = report.portfolio_alerts || [];
  const alertsWrap = $("daily-portfolio-alerts-wrap");
  const alertsList = $("daily-portfolio-alerts-list");
  if (alerts.length > 0) {
    alertsWrap.classList.remove("hidden");
    alertsList.innerHTML = alerts
      .map((item) => {
        const msgs = (item.alerts || [])
          .map((a) => `<li class="text-xs text-slate-200">• ${escapeHtml(a.message)}</li>`)
          .join("");
        const isSell = item.action === "SELL_ALL";
        return `
          <div class="rounded-xl p-3.5 border ${
            isSell ? "bg-rose-950/60 border-rose-500/50" : "bg-amber-950/50 border-amber-500/40"
          }">
            <div class="flex items-center justify-between mb-1.5">
              <span class="font-bold text-sm text-white">${escapeHtml(item.ticker)}</span>
              <span class="text-xs font-semibold tabular-nums ${
                item.pnl_pct >= 0 ? "text-emerald-300" : "text-rose-300"
              }">${item.pnl_pct >= 0 ? "+" : ""}${fmtNum(item.pnl_pct, 2)}%</span>
            </div>
            <ul class="space-y-1">${msgs}</ul>
          </div>`;
      })
      .join("");
  } else {
    alertsWrap.classList.add("hidden");
  }

  const candidates = report.buy_candidates || [];
  $("candidates-count").textContent = `${candidates.length} mã`;
  const listEl = $("buy-candidates-list");

  const liveCash = Math.max(0, Math.round(Number(accountState.cash_balance || 0)));
  const { buyFee } = getFeeRates();
  const cashHintEl = $("dashboard-cash-hint");
  if (cashHintEl) {
    cashHintEl.innerHTML =
      liveCash > 0
        ? `💵 Sức mua tiền mặt hiện có của bạn: <strong class="text-white">${fmtVnd(liveCash)}</strong>`
        : `💵 Bạn chưa nhập Tiền mặt hiện có (<button type="button" data-action="jump-sync-cash" class="underline text-emerald-300 font-semibold">Nhập số dư ngay</button> để AI tính đúng khối lượng mua)`;
    cashHintEl.querySelector('[data-action="jump-sync-cash"]')?.addEventListener("click", () => {
      document.querySelector('.tab-btn[data-tab="portfolio"]')?.click();
      openSyncCashModal();
    });
  }

  if (candidates.length === 0) {
    listEl.innerHTML = `
      <div class="glass-card rounded-2xl p-6 text-center text-xs text-slate-400">
        Phiên ${escapeHtml(report.date)} không có mã nào đạt đồng thời chuẩn BCTC &amp; điểm mua kỹ thuật.
      </div>`;
    return;
  }

  listEl.innerHTML = candidates
    .map((c) => {
      const g = c.gemini_analysis || {};
      const actualClose = Number(c.actual_close || 0);
      const unitCostWithFee = actualClose * (1 + buyFee);
      const liveMaxShares = unitCostWithFee > 0 && liveCash > 0 ? Math.floor(liveCash / unitCostWithFee) : 0;
      const liveMaxCost = Math.round(liveMaxShares * unitCostWithFee);

      const signalBadge =
        c.signal_type === "Pullback"
          ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">[Pullback]</span>`
          : `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-sky-500/20 text-sky-300 border border-sky-500/40">[Breakout]</span>`;

      let fitBadge = "";
      if (liveCash > 0 && actualClose > 0) {
        fitBadge =
          liveMaxShares > 0
            ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-violet-500/20 text-violet-300 border border-violet-500/40">[Mua tối đa ${liveMaxShares} CP]</span>`
            : `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/40">[Vượt tiền mặt hiện có]</span>`;
      } else if (c.fit_account ?? c.fit_500k) {
        fitBadge = `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-violet-500/20 text-violet-300 border border-violet-500/40">[Đủ thanh khoản]</span>`;
      }

      const missBadge = c.missing_bctc
        ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/40">[Thiếu BCTC mới nhất]</span>`
        : "";

      const liveCashSubnote =
        liveCash > 0 && actualClose > 0
          ? liveMaxShares > 0
            ? `<p class="text-[11px] text-emerald-300 mt-1.5 pt-1.5 border-t border-violet-500/25 tabular-nums">
                 📌 <strong>Theo tiền mặt hiện có (${fmtVnd(liveCash)}):</strong> Mua tối đa <strong>${liveMaxShares} CP</strong> (~${fmtVnd(liveMaxCost)}), hoặc chia 2 đợt mỗi đợt <strong>${Math.max(1, Math.floor(liveMaxShares / 2))} CP</strong>.
               </p>`
            : `<p class="text-[11px] text-rose-300 mt-1.5 pt-1.5 border-t border-violet-500/25 tabular-nums">
                 ⚠️ <strong>Theo tiền mặt hiện có (${fmtVnd(liveCash)}):</strong> Chưa đủ mua 1 CP (cần tối thiểu ${fmtVnd(unitCostWithFee)}).
               </p>`
          : "";

      // If old cached Gemini text still mentions "500k" or "500.000", replace or supplement with live account calculation
      let quantityAdviceText = g.suggested_buy_quantity || "";
      const hasLegacy500k = /500k|500\.000|500,000/i.test(quantityAdviceText);
      if ((!quantityAdviceText || hasLegacy500k) && liveCash > 0 && actualClose > 0) {
        quantityAdviceText =
          liveMaxShares > 0
            ? `Với số dư tiền mặt hiện có ${fmtVnd(liveCash)}, bạn mua được tối đa ${liveMaxShares} CP (~${fmtVnd(liveMaxCost)}). Khuyến nghị giải ngân thăm dò ${Math.max(1, Math.floor(liveMaxShares / 2))} CP trước, phần còn lại chờ xác nhận xu hướng.`
            : `Thị giá 1 CP (${fmtVnd(actualClose)}) cao hơn số dư tiền mặt hiện có của bạn (${fmtVnd(liveCash)}) — chưa đủ sức mua nếu không nạp thêm hoặc chốt mã khác.`;
      } else if (!quantityAdviceText) {
        quantityAdviceText =
          actualClose > 0
            ? `Đơn giá ~${fmtVnd(actualClose)}/CP (10 CP ~ ${fmtVnd(actualClose * 10)}). Hãy cập nhật Tiền mặt hiện có ở Tab Danh mục để hệ thống tính chính xác số CP cho tài khoản của bạn.`
            : "Chưa có dữ liệu.";
      }

      return `
        <article class="glass-card rounded-2xl p-4 space-y-3">
          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-lg font-extrabold text-white tracking-tight">${escapeHtml(c.ticker)}</span>
                <span class="text-[11px] text-slate-400">${escapeHtml(c.exchange)}</span>
                ${signalBadge}
                ${fitBadge}
                ${missBadge}
              </div>
              <p class="text-xs text-slate-400 mt-1">${escapeHtml(c.reason || "")}</p>
            </div>
            <div class="text-right shrink-0">
              <p class="text-base font-bold text-white tabular-nums">${fmtVnd(c.actual_close)}</p>
              <p class="text-[11px] text-emerald-400 font-semibold tabular-nums">
                BCTC ${fmtNum(c.fundamental_score, 0)}đ • KT ${fmtNum(c.technical_score, 0)}đ
              </p>
            </div>
          </div>

          <div class="grid grid-cols-4 gap-2 text-center bg-slate-950/60 rounded-xl p-2.5 text-[11px]">
            <div>
              <p class="text-slate-400">MA20</p>
              <p class="font-semibold text-slate-200 tabular-nums">${fmtNum(c.ma20, 0)}</p>
            </div>
            <div>
              <p class="text-slate-400">MA50</p>
              <p class="font-semibold text-slate-200 tabular-nums">${fmtNum(c.ma50, 0)}</p>
            </div>
            <div>
              <p class="text-slate-400">RSI(14)</p>
              <p class="font-semibold text-slate-200 tabular-nums">${fmtNum(c.rsi14, 1)}</p>
            </div>
            <div>
              <p class="text-slate-400">Vol/MA20</p>
              <p class="font-semibold text-slate-200 tabular-nums">${fmtNum(c.vol_ratio, 2)}x</p>
            </div>
          </div>

          <details class="group rounded-xl bg-slate-950/70 border border-slate-800 p-3">
            <summary class="cursor-pointer text-xs font-semibold text-sky-300 flex items-center justify-between list-none">
              <span>🤖 Hội đồng 3 AI phản biện (Bò vs Gấu vs Trọng tài)</span>
              <span class="text-slate-400 group-open:rotate-180 transition">▾</span>
            </summary>
            <div class="mt-3 space-y-2.5 text-xs border-t border-slate-800 pt-3">
              ${
                g.ai_bull_thesis
                  ? `<div class="rounded-lg bg-emerald-950/40 border border-emerald-500/30 p-2.5">
                      <p class="font-semibold text-emerald-300">🐂 AI 1 (Phe Bò — Luận điểm ủng hộ)</p>
                      <p class="text-slate-200 mt-0.5 leading-relaxed">${escapeHtml(g.ai_bull_thesis)}</p>
                    </div>`
                  : ""
              }
              <div class="rounded-lg bg-rose-950/30 border border-rose-500/25 p-2.5 space-y-2">
                <p class="font-semibold text-rose-300">🐻 AI 2 (Phe Gấu — Phản biện 4 rủi ro cốt lõi)</p>
                <div>
                  <p class="font-medium text-emerald-200">1. Chất lượng lợi nhuận (Core vs Đột biến)</p>
                  <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.profit_quality || "Chưa có dữ liệu.")}</p>
                </div>
                <div>
                  <p class="font-medium text-rose-200">2. Tin xấu / Kiện tụng / Lãnh đạo bán ra</p>
                  <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.negative_news || "Chưa có dữ liệu.")}</p>
                </div>
                <div>
                  <p class="font-medium text-amber-200">3. Bẫy cổ tức cổ phiếu / Phát hành thêm</p>
                  <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.dividend_trap || "Chưa có dữ liệu.")}</p>
                </div>
                <div>
                  <p class="font-medium text-sky-200">4. Rủi ro lớn nhất 1–3 tháng tới</p>
                  <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.biggest_risk || "Chưa có dữ liệu.")}</p>
                </div>
              </div>
              ${
                g.ai_referee_verdict
                  ? `<div class="rounded-lg bg-sky-950/40 border border-sky-500/30 p-2.5">
                      <p class="font-semibold text-sky-300">⚖️ AI 3 (Trọng tài — Chốt phương án theo tài khoản của bạn)</p>
                      <p class="text-slate-200 mt-0.5 leading-relaxed">${escapeHtml(g.ai_referee_verdict)}</p>
                    </div>`
                  : ""
              }
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div class="rounded-lg bg-amber-950/35 border border-amber-500/35 p-2.5">
                  <p class="font-semibold text-amber-300">🎯 Nên đặt giá bao nhiêu</p>
                  <p class="text-slate-200 mt-0.5 leading-relaxed">${escapeHtml(
                    g.suggested_buy_price ||
                      (c.actual_close
                        ? `Tham khảo quanh vùng ${fmtVnd(Math.min(c.actual_close, c.ma20 || c.actual_close))} – ${fmtVnd(c.actual_close)} (chạy quét lại để AI chốt SL/TP chi tiết).`
                        : "Chưa có dữ liệu.")
                  )}</p>
                </div>
                <div class="rounded-lg bg-violet-950/35 border border-violet-500/35 p-2.5">
                  <p class="font-semibold text-violet-300">💰 Nên mua bao nhiêu (Theo tài khoản hiện có)</p>
                  <p class="text-slate-200 mt-0.5 leading-relaxed">${escapeHtml(quantityAdviceText)}</p>
                  ${!hasLegacy500k ? liveCashSubnote : ""}
                </div>
              </div>
            </div>
          </details>
        </article>`;
    })
    .join("");
}

// -------------------- Account NAV, P&L, Win Rate & Cash Ratio --------------------
function getFeeRates() {
  return {
    buyFee: Number(accountState.fee_buy_pct || 0) / 100,
    sellFee: Number(accountState.fee_sell_pct || 0) / 100,
    sellTax: Number(accountState.tax_sell_pct ?? 0.1) / 100,
  };
}

function calcBreakEvenPrice(buyPrice) {
  const { buyFee, sellFee, sellTax } = getFeeRates();
  const denom = Math.max(0.5, 1 - sellFee - sellTax);
  return Math.ceil((Number(buyPrice) * (1 + buyFee)) / denom);
}

function calcNetHoldingPnl(buyPrice, latestPrice, qty) {
  const { buyFee, sellFee, sellTax } = getFeeRates();
  const totalCost = Math.round(buyPrice * qty * (1 + buyFee));
  const netProceeds = Math.round(latestPrice * qty * (1 - sellFee - sellTax));
  const netPnl = netProceeds - totalCost;
  const netPct = totalCost > 0 ? (netPnl / totalCost) * 100 : 0;
  return { totalCost, netProceeds, netPnl, netPct };
}

function getPortfolioSummaryMetrics() {
  const holdings = portfolioItems.filter((p) => (p.status || "holding") === "holding");
  let stockMarketValue = 0;
  let totalHoldingCost = 0;
  let unrealizedNetPnl = 0;

  for (const p of holdings) {
    const buyPrice = Number(p.buy_price || 0);
    const qty = Number(p.quantity || 0);
    const latestPrice = Number(p.latest_price || buyPrice);
    stockMarketValue += Math.round(latestPrice * qty);
    const { totalCost, netPnl } = calcNetHoldingPnl(buyPrice, latestPrice, qty);
    totalHoldingCost += totalCost;
    unrealizedNetPnl += netPnl;
  }

  const trades = Array.isArray(accountState.realized_trades) ? accountState.realized_trades : [];
  const monthKey = currentMonthKey();
  const realizedAllTime = trades.reduce((acc, t) => acc + Math.round(Number(t.net_pnl || 0)), 0);
  const realizedCostAllTime = trades.reduce(
    (acc, t) => acc + Math.round(Number(t.buy_price || 0) * Number(t.quantity || 0)),
    0
  );
  const realizedThisMonth = trades
    .filter((t) => String(t.sell_date || "").startsWith(monthKey))
    .reduce((acc, t) => acc + Math.round(Number(t.net_pnl || 0)), 0);
  const realizedCostThisMonth = trades
    .filter((t) => String(t.sell_date || "").startsWith(monthKey))
    .reduce((acc, t) => acc + Math.round(Number(t.buy_price || 0) * Number(t.quantity || 0)), 0);

  return {
    holdings,
    trades,
    monthKey,
    stockMarketValue,
    totalHoldingCost,
    unrealizedNetPnl,
    realizedAllTime,
    realizedCostAllTime,
    realizedThisMonth,
    realizedCostThisMonth,
  };
}

function renderAccountSummary() {
  const {
    monthKey,
    trades,
    stockMarketValue,
    totalHoldingCost,
    unrealizedNetPnl,
    realizedAllTime,
    realizedCostAllTime,
    realizedThisMonth,
    realizedCostThisMonth,
  } = getPortfolioSummaryMetrics();

  const cashBalance = Math.max(0, Math.round(Number(accountState.cash_balance || 0)));
  const nav = cashBalance + stockMarketValue;

  // Implied active original capital = Current Cash + Cost Basis of Holding Stocks - Realized Net Profit
  const impliedBaseCapital = Math.max(0, cashBalance + totalHoldingCost - realizedAllTime);
  const recordedDeposited = Math.max(0, Math.round(Number(accountState.total_deposited || 0)));
  const effectiveCapitalBase = recordedDeposited > 0 ? recordedDeposited : impliedBaseCapital;

  const stockPct = nav > 0 ? Math.round((stockMarketValue / nav) * 100) : 0;
  const cashPct = nav > 0 ? 100 - stockPct : 100;

  // Bottom-up transaction-based P&L (immune to cash deposit/sync mismatches)
  const allTimePnl = realizedAllTime + unrealizedNetPnl;
  const allTimeDenominator =
    effectiveCapitalBase > 0
      ? effectiveCapitalBase
      : totalHoldingCost > 0
      ? totalHoldingCost
      : realizedCostAllTime;
  const allTimePct = allTimeDenominator > 0 ? (allTimePnl / allTimeDenominator) * 100 : 0;

  const monthPnl = realizedThisMonth + unrealizedNetPnl;
  const monthDenominator =
    effectiveCapitalBase > 0
      ? effectiveCapitalBase
      : totalHoldingCost + realizedCostThisMonth;
  const monthPct = monthDenominator > 0 ? (monthPnl / monthDenominator) * 100 : 0;

  // Render NAV & Cash/Stock cards
  $("acc-nav").textContent = fmtVnd(nav);
  if (recordedDeposited > 0 && Math.abs(recordedDeposited - impliedBaseCapital) > 1000) {
    $("acc-deposited-line").textContent = `Vốn gốc: ${fmtVnd(impliedBaseCapital)} (Đã ghi nạp: ${fmtVnd(recordedDeposited)})`;
  } else {
    $("acc-deposited-line").textContent = `Tổng vốn gốc đang đầu tư: ${fmtVnd(effectiveCapitalBase)}`;
  }

  $("acc-cash-val").textContent = fmtVnd(cashBalance);
  $("acc-stock-val").textContent = fmtVnd(stockMarketValue);
  if ($("acc-stock-cost-line")) {
    $("acc-stock-cost-line").textContent =
      totalHoldingCost > 0
        ? `Vốn mua: ${fmtVnd(totalHoldingCost)} (${unrealizedNetPnl >= 0 ? "+" : ""}${fmtVnd(unrealizedNetPnl)})`
        : "Chưa giữ cổ phiếu nào";
  }
  $("acc-stock-pct").textContent = `${stockPct}%`;
  $("acc-cash-pct").textContent = `${cashPct}%`;
  $("acc-stock-bar").style.width = `${stockPct}%`;
  $("acc-cash-bar").style.width = `${cashPct}%`;

  $("acc-month-label").textContent = `Lời/lỗ tháng ${monthKey}`;
  $("acc-month-pnl").textContent = fmtSignedVnd(monthPnl);
  $("acc-month-pnl").className = `text-sm font-bold tabular-nums mt-0.5 ${
    monthPnl >= 0 ? "text-emerald-400" : "text-rose-400"
  }`;
  $("acc-month-pnl-pct").textContent = `${monthPct >= 0 ? "+" : ""}${fmtNum(monthPct, 2)}% (Chốt tháng: ${fmtSignedVnd(realizedThisMonth)})`;
  $("acc-month-pnl-pct").className = `text-[11px] tabular-nums ${
    monthPnl >= 0 ? "text-emerald-300/80" : "text-rose-300/80"
  }`;

  $("acc-alltime-pnl").textContent = fmtSignedVnd(allTimePnl);
  $("acc-alltime-pnl").className = `text-sm font-bold tabular-nums mt-0.5 ${
    allTimePnl >= 0 ? "text-emerald-400" : "text-rose-400"
  }`;
  $("acc-alltime-pnl-pct").textContent = `${allTimePct >= 0 ? "+" : ""}${fmtNum(allTimePct, 2)}% (Đã chốt: ${fmtSignedVnd(realizedAllTime)} • Đang giữ: ${fmtSignedVnd(unrealizedNetPnl)})`;
  $("acc-alltime-pnl-pct").className = `text-[11px] tabular-nums ${
    allTimePnl >= 0 ? "text-emerald-300/80" : "text-rose-300/80"
  }`;

  // Cash ratio warning when VN-Index < MA50 & Stock > 60%
  const vniWarning = Boolean(latestReport?.vnindex?.warning_flag);
  const warnBox = $("cash-ratio-warning");
  if (vniWarning && nav > 0 && stockPct > 60) {
    $("cash-ratio-warning-text").textContent =
      `VN-Index đang dưới MA50 (rủi ro cao), nhưng tỷ trọng cổ phiếu hiện tại là ${stockPct}% (> 60% NAV). Nên cân nhắc hạ bớt tỷ trọng và giữ tiền mặt phòng thủ.`;
    warnBox.classList.remove("hidden");
  } else {
    warnBox.classList.add("hidden");
  }

  renderPerformanceStats(trades);
}

function renderPerformanceStats(trades) {
  const total = trades.length;
  $("perf-summary-badge").textContent = `${total} lệnh chốt ▾`;

  const wins = trades.filter((t) => Number(t.net_pnl || 0) > 0);
  const losses = trades.filter((t) => Number(t.net_pnl || 0) <= 0);
  const winRate = total > 0 ? (wins.length / total) * 100 : null;
  const avgWin = wins.length > 0 ? wins.reduce((a, t) => a + Number(t.net_pnl_pct || 0), 0) / wins.length : null;
  const avgLoss = losses.length > 0 ? losses.reduce((a, t) => a + Number(t.net_pnl_pct || 0), 0) / losses.length : null;

  $("perf-winrate").textContent = winRate !== null ? `${fmtNum(winRate, 1)}%` : "--";
  $("perf-wl").textContent = `${wins.length} / ${losses.length}`;
  $("perf-avg-win").textContent = avgWin !== null ? `+${fmtNum(avgWin, 1)}%` : "--";
  $("perf-avg-loss").textContent = avgLoss !== null ? `${fmtNum(avgLoss, 1)}%` : "--";

  const listEl = $("realized-trades-list");
  if (total === 0) {
    listEl.innerHTML = `<p class="text-slate-500 text-center py-2">Chưa có lệnh chốt lời / cắt lỗ nào.</p>`;
    return;
  }

  const sorted = [...trades].sort((a, b) => String(b.sell_date || "").localeCompare(String(a.sell_date || "")));
  listEl.innerHTML = sorted
    .slice(0, 15)
    .map((t) => {
      const pos = Number(t.net_pnl || 0) >= 0;
      return `
        <div class="flex items-center justify-between bg-slate-950/60 rounded-lg px-2.5 py-1.5">
          <div>
            <span class="font-bold text-white">${escapeHtml(t.ticker)}</span>
            <span class="text-slate-400"> • ${t.quantity} CP • ${escapeHtml(t.sell_date || "")}</span>
          </div>
          <span class="font-semibold tabular-nums ${pos ? "text-emerald-400" : "text-rose-400"}">
            ${fmtSignedVnd(t.net_pnl)} (${pos ? "+" : ""}${fmtNum(t.net_pnl_pct, 1)}%)
          </span>
        </div>`;
    })
    .join("");
}

// -------------------- Portfolio CRUD --------------------
async function loadPortfolio() {
  try {
    const snap = await getDocs(collection(db, "portfolio"));
    portfolioItems = [];
    snap.forEach((d) => {
      portfolioItems.push({ ticker: d.id, ...d.data() });
    });
    portfolioItems.sort((a, b) => (b.buy_date || "").localeCompare(a.buy_date || ""));
  } catch (err) {
    console.error("Failed to load portfolio:", err);
    $("portfolio-list").innerHTML = `
      <div class="rounded-2xl bg-rose-950/60 border border-rose-500/40 p-4 text-xs text-rose-200 space-y-1">
        <p class="font-bold text-rose-300">Không thể kết nối Firestore (${escapeHtml(err.code || "error")})</p>
        <p>${escapeHtml(err.message || err)}</p>
      </div>`;
  }
}

function renderPortfolio() {
  const listEl = $("portfolio-list");
  const filtered = portfolioItems.filter((p) => (p.status || "holding") === currentPfFilter);

  if (filtered.length === 0) {
    listEl.innerHTML = `
      <div class="glass-card rounded-2xl p-6 text-center text-xs text-slate-400">
        Không có mã nào trong mục "${currentPfFilter === "holding" ? "Đang nắm giữ" : "Đã đóng"}".
      </div>`;
    return;
  }

  listEl.innerHTML = filtered
    .map((p) => {
      const buyPrice = Number(p.buy_price || 0);
      const qty = Number(p.quantity || 0);
      const latestPrice = Number(p.latest_price || buyPrice);
      const highestPrice = Number(p.highest_price || Math.max(buyPrice, latestPrice));
      const ma50 = Number(p.latest_ma50 || 0);
      const breakEven = calcBreakEvenPrice(buyPrice);
      const { netPnl, netPct } = calcNetHoldingPnl(buyPrice, latestPrice, qty);
      const trailingStopPrice = Math.round(highestPrice * 0.92);
      const belowMa50 = ma50 > 0 && latestPrice < ma50;

      return `
        <div class="glass-card rounded-2xl p-4 space-y-3">
          <div class="flex items-start justify-between">
            <div>
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-base font-extrabold text-white">${escapeHtml(p.ticker)}</span>
                <span class="text-[11px] text-slate-400">${escapeHtml(p.exchange || "HOSE")}</span>
                ${
                  p.sold_half
                    ? `<span class="px-2 py-0.5 rounded-md text-[10px] font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/40">Đã chốt 1/2</span>`
                    : ""
                }
                ${
                  belowMa50
                    ? `<span class="px-2 py-0.5 rounded-md text-[10px] font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/40">Dưới MA50</span>`
                    : ""
                }
              </div>
              <p class="text-[11px] text-slate-400 mt-0.5 tabular-nums">
                SL: ${qty} CP • Mua: ${fmtVnd(buyPrice)} • Hòa vốn: <span class="text-slate-200">${fmtVnd(breakEven)}</span>
              </p>
            </div>
            <div class="text-right">
              <p class="text-sm font-bold tabular-nums ${
                netPct >= 0 ? "text-emerald-400" : "text-rose-400"
              }">${netPct >= 0 ? "+" : ""}${fmtNum(netPct, 2)}%</p>
              <p class="text-[11px] tabular-nums ${
                netPnl >= 0 ? "text-emerald-300/80" : "text-rose-300/80"
              }">${fmtSignedVnd(netPnl)} <span class="text-[10px] text-slate-500">(ròng)</span></p>
            </div>
          </div>

          <div class="grid grid-cols-3 gap-2 bg-slate-950/60 rounded-xl p-2.5 text-[11px] text-center">
            <div>
              <p class="text-slate-400">Giá gần nhất</p>
              <p class="font-semibold text-white tabular-nums">${fmtVnd(latestPrice)}</p>
            </div>
            <div>
              <p class="text-slate-400">MA50</p>
              <p class="font-semibold ${belowMa50 ? "text-rose-400" : "text-emerald-300"} tabular-nums">
                ${ma50 > 0 ? fmtVnd(ma50) : "Chờ quét"}
              </p>
            </div>
            <div>
              <p class="text-slate-400">${p.sold_half ? "Trailing (-8%)" : "Đỉnh cao nhất"}</p>
              <p class="font-semibold text-amber-300 tabular-nums">
                ${p.sold_half ? fmtVnd(trailingStopPrice) : fmtVnd(highestPrice)}
              </p>
            </div>
          </div>

          ${
            p.notes
              ? `<p class="text-xs text-slate-400 italic">"${escapeHtml(p.notes)}"</p>`
              : ""
          }

          <div class="flex flex-wrap gap-2 pt-1">
            ${
              p.status !== "closed"
                ? `<button data-action="sell" data-ticker="${escapeHtml(p.ticker)}"
                     class="px-3 py-1.5 rounded-lg text-[11px] font-semibold bg-amber-600/25 hover:bg-amber-600/40 text-amber-300 border border-amber-500/40">
                     💰 Bán / Chốt lời
                   </button>`
                : ""
            }
            <button data-action="edit" data-ticker="${escapeHtml(p.ticker)}"
                    class="px-3 py-1.5 rounded-lg text-[11px] font-semibold bg-slate-800 hover:bg-slate-700 text-sky-300">
              Sửa
            </button>
            <button data-action="delete" data-ticker="${escapeHtml(p.ticker)}"
                    class="px-3 py-1.5 rounded-lg text-[11px] font-semibold bg-rose-950/60 hover:bg-rose-900/60 text-rose-300 border border-rose-500/30">
              Xóa
            </button>
          </div>
        </div>`;
    })
    .join("");

  listEl.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      const ticker = btn.dataset.ticker;
      const item = portfolioItems.find((x) => x.ticker === ticker);
      if (!item) return;

      if (action === "edit") {
        openPortfolioModal(item);
      } else if (action === "sell") {
        openSellModal(item);
      } else if (action === "delete") {
        if (confirm(`Xóa mã ${ticker} khỏi danh mục Firestore?`)) {
          const isHolding = (item.status || "holding") === "holding";
          const { buyFee } = getFeeRates();
          const itemCost = Math.round(Number(item.buy_price || 0) * Number(item.quantity || 0) * (1 + buyFee));
          await deleteDoc(doc(db, "portfolio", ticker));
          if (isHolding && itemCost > 0) {
            const refund = confirm(
              `Bạn có muốn hoàn lại tiền gốc mua ${ticker} (${fmtVnd(itemCost)}) vào Tiền mặt hiện có không?`
            );
            if (refund) {
              const newCash = Math.round(Number(accountState.cash_balance || 0) + itemCost);
              await persistAccount({ cash_balance: newCash });
            }
          }
          showToast(`Đã xóa ${ticker}.`);
          await loadPortfolio();
          renderAccountSummary();
          renderPortfolio();
          if (latestReport) renderDailyReport(latestReport);
        }
      }
    });
  });
}

function openPortfolioModal(item) {
  const isEdit = Boolean(item);
  $("modal-title").textContent = isEdit ? `Cập nhật ${item.ticker}` : "Nhập lệnh Mua";
  $("pf-ticker").value = item ? item.ticker : "";
  $("pf-ticker").disabled = isEdit;
  $("pf-exchange").value = item?.exchange || "HOSE";
  $("pf-buy-price").value = item?.buy_price || "";
  $("pf-quantity").value = item?.quantity || "";
  $("pf-buy-date").value = item?.buy_date || todayIso();
  $("pf-status").value = item?.status || "holding";
  $("pf-sold-half").checked = Boolean(item?.sold_half);
  $("pf-notes").value = item?.notes || "";
  $("pf-deduct-cash-wrap").classList.remove("hidden");
  const deductLabel = document.querySelector('label[for="pf-deduct-cash"]');
  if (deductLabel) {
    deductLabel.textContent = isEdit
      ? "Tự động bù/trừ phần chênh lệch giá vốn vào Tiền mặt hiện có"
      : "Tự động trừ tiền mặt hiện có (nếu thiếu tự tính là nạp thêm vốn)";
  }
  $("pf-deduct-cash").checked = true;
  $("pf-form-error").classList.add("hidden");
  $("pf-form-error").textContent = "";
  $("portfolio-modal").classList.remove("hidden");
}

function closePortfolioModal() {
  $("portfolio-modal").classList.add("hidden");
}

async function handleSavePortfolio(e) {
  e.preventDefault();
  const ticker = $("pf-ticker").value.trim().toUpperCase();
  if (!ticker) return;

  const errEl = $("pf-form-error");
  const btn = $("btn-save-portfolio");
  errEl.classList.add("hidden");
  btn.disabled = true;
  btn.textContent = "Đang lưu...";

  const isEdit = $("pf-ticker").disabled;
  const buyPrice = Number($("pf-buy-price").value);
  const qty = Number($("pf-quantity").value);
  const existing = portfolioItems.find((x) => x.ticker === ticker);
  const highestPrice = Math.max(Number(existing?.highest_price || 0), buyPrice);

  const payload = {
    ticker,
    exchange: $("pf-exchange").value,
    buy_price: buyPrice,
    quantity: qty,
    buy_date: $("pf-buy-date").value,
    highest_price: highestPrice,
    sold_half: $("pf-sold-half").checked,
    status: $("pf-status").value,
    notes: $("pf-notes").value.trim(),
    updated_at: new Date().toISOString(),
  };

  try {
    const { buyFee } = getFeeRates();
    const newCost = payload.status === "holding" ? Math.round(buyPrice * qty * (1 + buyFee)) : 0;
    const oldCost =
      existing && (existing.status || "holding") === "holding"
        ? Math.round(Number(existing.buy_price || 0) * Number(existing.quantity || 0) * (1 + buyFee))
        : 0;

    await setDoc(doc(db, "portfolio", ticker), payload, { merge: true });

    // Reconcile cash balance and net deposited capital accurately
    if ($("pf-deduct-cash").checked) {
      const costDelta = isEdit ? newCost - oldCost : newCost;
      if (costDelta !== 0) {
        const currentCash = Math.round(Number(accountState.cash_balance || 0));
        const currentDeposited = Math.round(Number(accountState.total_deposited || 0));
        if (costDelta > 0) {
          if (currentCash >= costDelta) {
            await persistAccount({ cash_balance: currentCash - costDelta });
          } else {
            // Buying more than available cash -> shortfall comes from newly added external capital
            const shortfall = costDelta - currentCash;
            await persistAccount({
              cash_balance: 0,
              total_deposited: currentDeposited > 0 ? currentDeposited + shortfall : 0,
            });
          }
        } else {
          // Cost reduced (e.g. edited smaller qty or lower price) -> refund difference to cash
          await persistAccount({ cash_balance: currentCash + Math.abs(costDelta) });
        }
      }
    }

    closePortfolioModal();
    showToast(`Đã lưu lệnh ${ticker} vào Firestore!`);
    await loadPortfolio();
    renderAccountSummary();
    renderPortfolio();
    if (latestReport) renderDailyReport(latestReport);
  } catch (err) {
    errEl.textContent = `Lỗi lưu Firestore: ${err.code ? `[${err.code}] ` : ""}${err.message || err}`;
    errEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Lưu vào Firestore";
  }
}

// -------------------- Deposit / Withdraw & Sync Cash --------------------
function openCashflowModal() {
  $("cf-type").value = "deposit";
  $("cf-date").value = todayIso();
  $("cf-amount").value = "";
  $("cf-note").value = "";
  renderCashflowHistory();
  $("cashflow-modal").classList.remove("hidden");
}

function renderCashflowHistory() {
  const listEl = $("cashflow-history-list");
  const flows = [...(accountState.cash_flows || [])].sort((a, b) =>
    String(b.date || "").localeCompare(String(a.date || ""))
  );
  if (flows.length === 0) {
    listEl.innerHTML = `<p class="text-slate-500 text-center py-2">Chưa có giao dịch nạp/rút nào.</p>`;
    return;
  }
  listEl.innerHTML = flows
    .slice(0, 10)
    .map((cf) => {
      const isDep = cf.type !== "withdraw";
      return `
        <div class="flex items-center justify-between bg-slate-950/60 rounded-lg px-2.5 py-1.5">
          <div>
            <span class="${isDep ? "text-emerald-300" : "text-rose-300"} font-semibold">${isDep ? "Nạp" : "Rút"}</span>
            <span class="text-slate-400"> • ${escapeHtml(cf.date || "")}</span>
            ${cf.note ? `<p class="text-[10px] text-slate-500">${escapeHtml(cf.note)}</p>` : ""}
          </div>
          <span class="font-semibold tabular-nums ${isDep ? "text-emerald-400" : "text-rose-400"}">
            ${isDep ? "+" : "-"}${fmtVnd(cf.amount)}
          </span>
        </div>`;
    })
    .join("");
}

async function handleSaveCashflow(e) {
  e.preventDefault();
  const type = $("cf-type").value;
  const amount = Math.round(Number($("cf-amount").value));
  const date = $("cf-date").value;
  const note = $("cf-note").value.trim();
  if (!amount || amount <= 0) return;

  const { totalHoldingCost, realizedAllTime } = getPortfolioSummaryMetrics();
  const currentCash = Math.round(Number(accountState.cash_balance || 0));
  const currentDeposited = Math.round(Number(accountState.total_deposited || 0));
  const baseDeposited =
    currentDeposited > 0 ? currentDeposited : Math.max(0, currentCash + totalHoldingCost - realizedAllTime);

  const delta = type === "withdraw" ? -amount : amount;
  const newCash = Math.max(0, currentCash + delta);
  const newDeposited = Math.max(0, baseDeposited + delta);
  const newFlows = [
    ...(accountState.cash_flows || []),
    { id: Date.now().toString(), type, amount, date, note },
  ];

  await persistAccount({
    cash_balance: newCash,
    total_deposited: newDeposited,
    cash_flows: newFlows,
  });

  $("cashflow-modal").classList.add("hidden");
  showToast(type === "deposit" ? `Đã nạp +${fmtVnd(amount)}` : `Đã rút -${fmtVnd(amount)}`);
  renderAccountSummary();
  if (latestReport) renderDailyReport(latestReport);
}

function calcImpliedDepositedForCash(cashAmount) {
  const { totalHoldingCost, realizedAllTime } = getPortfolioSummaryMetrics();
  return Math.max(0, Math.round(Number(cashAmount || 0)) + totalHoldingCost - realizedAllTime);
}

function updateSyncCashPreview() {
  const cashInput = Math.max(0, Math.round(Number($("sync-cash-balance")?.value || 0)));
  const { stockMarketValue, totalHoldingCost, realizedAllTime } = getPortfolioSummaryMetrics();
  const impliedDeposited = calcImpliedDepositedForCash(cashInput);
  const previewNav = cashInput + stockMarketValue;
  const previewEl = $("sync-cash-preview");
  if (previewEl) {
    previewEl.textContent = `Gợi ý khớp tự động: Tiền mặt (${fmtVnd(cashInput)}) + Giá vốn CP đang giữ (${fmtVnd(totalHoldingCost)}) - Lãi đã chốt (${fmtSignedVnd(realizedAllTime)}) = Vốn gốc ${fmtVnd(impliedDeposited)} • Tổng NAV sẽ hiển thị: ${fmtVnd(previewNav)}.`;
  }
}

function handleAutoCalcDeposited() {
  const cashInput = Math.max(0, Math.round(Number($("sync-cash-balance")?.value || 0)));
  $("sync-total-deposited").value = calcImpliedDepositedForCash(cashInput);
  updateSyncCashPreview();
}

function openSyncCashModal() {
  const currentCash = Math.round(Number(accountState.cash_balance || 0));
  const currentDeposited = Math.round(Number(accountState.total_deposited || 0));
  $("sync-cash-balance").value = currentCash;
  $("sync-total-deposited").value =
    currentDeposited > 0 ? currentDeposited : calcImpliedDepositedForCash(currentCash);
  updateSyncCashPreview();
  $("sync-cash-modal").classList.remove("hidden");
}

async function handleSaveSyncCash(e) {
  e.preventDefault();
  const cashBalance = Math.max(0, Math.round(Number($("sync-cash-balance").value || 0)));
  let totalDeposited = Math.max(0, Math.round(Number($("sync-total-deposited").value || 0)));
  if (totalDeposited === 0 && cashBalance > 0) {
    totalDeposited = calcImpliedDepositedForCash(cashBalance);
  }
  await persistAccount({
    cash_balance: cashBalance,
    total_deposited: totalDeposited,
  });
  $("sync-cash-modal").classList.add("hidden");
  showToast(`✅ Đã cập nhật Tiền mặt khả dụng: ${fmtVnd(cashBalance)}!`);
  renderAccountSummary();
  if (latestReport) renderDailyReport(latestReport);
}

// -------------------- Sell / Take-Profit Modal --------------------
let activeSellItem = null;

function openSellModal(item) {
  activeSellItem = item;
  $("sell-modal-title").textContent = `Bán / Chốt lời ${item.ticker} (đang giữ ${item.quantity} CP)`;
  $("sell-ticker").value = item.ticker;
  $("sell-price").value = Math.round(Number(item.latest_price || item.buy_price || 0));
  $("sell-qty").max = Number(item.quantity || 1);
  $("sell-qty").value = Number(item.quantity || 1);
  $("sell-date").value = todayIso();

  $("btn-sell-half-preset").onclick = () => {
    $("sell-qty").value = Math.max(1, Math.floor(Number(item.quantity || 1) / 2));
    updateSellPreview();
  };
  $("btn-sell-all-preset").onclick = () => {
    $("sell-qty").value = Number(item.quantity || 1);
    updateSellPreview();
  };

  updateSellPreview();
  $("sell-modal").classList.remove("hidden");
}

function updateSellPreview() {
  if (!activeSellItem) return;
  const sellPrice = Number($("sell-price").value || 0);
  const sellQty = Number($("sell-qty").value || 0);
  const buyPrice = Number(activeSellItem.buy_price || 0);
  if (sellPrice <= 0 || sellQty <= 0) return;

  const { buyFee, sellFee, sellTax } = getFeeRates();
  const grossProceeds = sellPrice * sellQty;
  const sellFeeTax = Math.round(grossProceeds * (sellFee + sellTax));
  const netProceeds = Math.round(grossProceeds - sellFeeTax);
  const costBasis = Math.round(buyPrice * sellQty * (1 + buyFee));
  const netPnl = netProceeds - costBasis;
  const netPct = costBasis > 0 ? (netPnl / costBasis) * 100 : 0;

  $("sell-preview").innerHTML = `
    <div class="flex justify-between"><span class="text-slate-400">Tiền bán gộp:</span><span class="text-white tabular-nums">${fmtVnd(grossProceeds)}</span></div>
    <div class="flex justify-between"><span class="text-slate-400">Phí bán + Thuế (0.1%):</span><span class="text-rose-300 tabular-nums">-${fmtVnd(sellFeeTax)}</span></div>
    <div class="flex justify-between font-semibold"><span class="text-slate-200">Tiền thực nhận về ví:</span><span class="text-sky-300 tabular-nums">${fmtVnd(netProceeds)}</span></div>
    <div class="flex justify-between font-bold border-t border-slate-800 pt-1.5 mt-1">
      <span class="text-slate-200">Lãi/Lỗ ròng lệnh này:</span>
      <span class="tabular-nums ${netPnl >= 0 ? "text-emerald-400" : "text-rose-400"}">${fmtSignedVnd(netPnl)} (${netPnl >= 0 ? "+" : ""}${fmtNum(netPct, 2)}%)</span>
    </div>`;
}

async function handleConfirmSell(e) {
  e.preventDefault();
  if (!activeSellItem) return;
  const ticker = activeSellItem.ticker;
  const sellPrice = Number($("sell-price").value);
  const sellQty = Number($("sell-qty").value);
  const sellDate = $("sell-date").value;
  const holdingQty = Number(activeSellItem.quantity || 0);
  if (sellPrice <= 0 || sellQty <= 0 || sellQty > holdingQty) {
    alert("Số lượng bán không hợp lệ.");
    return;
  }

  const { buyFee, sellFee, sellTax } = getFeeRates();
  const grossProceeds = sellPrice * sellQty;
  const sellFeeTax = Math.round(grossProceeds * (sellFee + sellTax));
  const netProceeds = Math.round(grossProceeds - sellFeeTax);
  const costBasis = Math.round(Number(activeSellItem.buy_price || 0) * sellQty * (1 + buyFee));
  const netPnl = netProceeds - costBasis;
  const netPct = costBasis > 0 ? (netPnl / costBasis) * 100 : 0;

  const remainingQty = holdingQty - sellQty;
  if (remainingQty > 0) {
    await setDoc(
      doc(db, "portfolio", ticker),
      { quantity: remainingQty, sold_half: true, updated_at: new Date().toISOString() },
      { merge: true }
    );
  } else {
    await setDoc(
      doc(db, "portfolio", ticker),
      { status: "closed", sell_price: sellPrice, sell_date: sellDate, updated_at: new Date().toISOString() },
      { merge: true }
    );
  }

  const newTrade = {
    id: Date.now().toString(),
    ticker,
    buy_price: Number(activeSellItem.buy_price || 0),
    sell_price: sellPrice,
    quantity: sellQty,
    buy_date: activeSellItem.buy_date || "",
    sell_date: sellDate,
    fee_tax: sellFeeTax,
    net_pnl: netPnl,
    net_pnl_pct: Number(netPct.toFixed(2)),
  };

  await persistAccount({
    cash_balance: Number(accountState.cash_balance || 0) + netProceeds,
    realized_trades: [...(accountState.realized_trades || []), newTrade],
  });

  $("sell-modal").classList.add("hidden");
  showToast(`Đã bán ${sellQty} CP ${ticker}, cộng ${fmtVnd(netProceeds)} về tiền mặt!`);
  await loadPortfolio();
  renderAccountSummary();
  renderPortfolio();
  if (latestReport) renderDailyReport(latestReport);
}

// -------------------- Fee Settings --------------------
async function handleSaveFeeSettings(e) {
  e.preventDefault();
  await persistAccount({
    fee_buy_pct: Math.max(0, Number($("set-fee-buy").value || 0)),
    fee_sell_pct: Math.max(0, Number($("set-fee-sell").value || 0)),
    tax_sell_pct: Math.max(0, Number($("set-tax-sell").value || 0)),
  });
  showToast("Đã lưu cấu hình phí giao dịch & thuế bán!");
  renderAccountSummary();
  renderPortfolio();
}

// -------------------- FCM Push Notifications (Native Android APK + Web Fallback) --------------------
function getNativePushPlugin() {
  const cap = window.Capacitor;
  if (cap && typeof cap.isNativePlatform === "function" && cap.isNativePlatform()) {
    return cap.Plugins?.PushNotifications || null;
  }
  return null;
}

let nativePushListenersBound = false;

async function saveFcmTokenToFirestore(token) {
  await setDoc(
    doc(db, "settings", "fcm_tokens"),
    {
      tokens: arrayUnion(token),
      updated_at: new Date().toISOString(),
    },
    { merge: true }
  );
}

async function loadFcmSettings() {
  const badge = $("fcm-status-badge");
  const nativePush = getNativePushPlugin();

  if (nativePush) {
    try {
      const perm = await nativePush.checkPermissions();
      if (perm.receive === "granted") {
        badge.textContent = "APK Native • Đã bật";
        badge.className = "px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-500/20 text-emerald-300 shrink-0";
      } else if (perm.receive === "denied") {
        badge.textContent = "APK • Bị chặn";
        badge.className = "px-2.5 py-1 rounded-full text-[11px] font-semibold bg-rose-500/20 text-rose-300 shrink-0";
      } else {
        badge.textContent = "APK Native • Chưa bật";
        badge.className = "px-2.5 py-1 rounded-full text-[11px] font-semibold bg-sky-500/20 text-sky-300 shrink-0";
      }
    } catch {
      badge.textContent = "APK Native";
    }
  } else if (!("Notification" in window)) {
    badge.textContent = "Không hỗ trợ";
  } else if (Notification.permission === "granted") {
    badge.textContent = "Đã cấp quyền";
    badge.className = "px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-500/20 text-emerald-300 shrink-0";
  } else if (Notification.permission === "denied") {
    badge.textContent = "Bị chặn";
    badge.className = "px-2.5 py-1 rounded-full text-[11px] font-semibold bg-rose-500/20 text-rose-300 shrink-0";
  } else {
    badge.textContent = "Chưa bật";
  }

  try {
    const snap = await getDoc(doc(db, "settings", "fcm_tokens"));
    const tokens = snap.exists() ? snap.data().tokens || [] : [];
    $("fcm-token-count").textContent = `Số thiết bị đã đăng ký: ${tokens.length}`;
  } catch (err) {
    console.warn("Could not read settings/fcm_tokens:", err);
  }
}

async function registerNativeAndroidPush(nativePush, feedback) {
  const perm = await nativePush.requestPermissions();
  if (perm.receive !== "granted") {
    feedback.className = "text-xs text-rose-400";
    feedback.textContent = "Bạn chưa cấp quyền thông báo cho ứng dụng SwingBot VN.";
    return;
  }

  // Create strictly silent Android Notification Channel (importance 2 = LOW: no sound, no vibration)
  if (typeof nativePush.createChannel === "function") {
    try {
      await nativePush.createChannel({
        id: "swingbot_silent",
        name: "SwingBot Im lặng",
        description: "Thông báo chứng khoán im lặng tuyệt đối (không âm thanh, không rung)",
        importance: 2,
        visibility: 1,
        vibration: false,
        lights: false,
      });
    } catch (e) {
      console.warn("createChannel warning:", e);
    }
  }

  if (!nativePushListenersBound) {
    nativePushListenersBound = true;

    nativePush.addListener("pushNotificationReceived", (notification) => {
      showToast(`${notification.title || "SwingBot"}: ${notification.body || ""}`, 6000);
    });

    nativePush.addListener("pushNotificationActionPerformed", () => {
      refreshAllData();
    });
  }

  const token = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("Hết thời gian lấy Native FCM Token (15s). Kiểm tra Google Play Services.")),
      15000
    );
    const regHandle = nativePush.addListener("registration", (t) => {
      clearTimeout(timer);
      regHandle?.remove?.();
      errHandle?.remove?.();
      resolve(t.value);
    });
    const errHandle = nativePush.addListener("registrationError", (err) => {
      clearTimeout(timer);
      regHandle?.remove?.();
      errHandle?.remove?.();
      reject(new Error(JSON.stringify(err)));
    });
    nativePush.register();
  });

  if (!token) throw new Error("Không lấy được Native FCM token.");
  await saveFcmTokenToFirestore(token);
  feedback.className = "text-xs text-emerald-400";
  feedback.textContent = "✅ Đã đăng ký Native Push (APK) thành công! Máy sẽ nhận thông báo tức thì kể cả khi khóa màn hình.";
  await loadFcmSettings();
}

async function handleRegisterFcmToken() {
  const feedback = $("fcm-feedback");
  const btn = $("btn-enable-fcm");
  feedback.classList.remove("hidden");

  btn.disabled = true;
  btn.textContent = "Đang đăng ký Push Notification...";

  try {
    const nativePush = getNativePushPlugin();
    if (nativePush) {
      await registerNativeAndroidPush(nativePush, feedback);
      return;
    }

    if (!messaging) {
      feedback.className = "text-xs text-rose-400";
      feedback.textContent = "Trình duyệt này không hỗ trợ Firebase Messaging. Hãy mở bằng app SwingBot VN (.apk) để nhận thông báo ổn định nhất.";
      return;
    }
    if (!FCM_VAPID_KEY || FCM_VAPID_KEY.startsWith("REPLACE_")) {
      feedback.className = "text-xs text-amber-300";
      feedback.textContent = "Chưa điền FCM_VAPID_KEY trong frontend/firebase-config.js.";
      return;
    }

    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      feedback.className = "text-xs text-rose-400";
      feedback.textContent = "Bạn chưa cấp quyền hiển thị thông báo.";
      return;
    }

    const activeReg = await ensureActiveServiceWorker();
    const token = await getToken(messaging, {
      vapidKey: FCM_VAPID_KEY,
      serviceWorkerRegistration: activeReg,
    });
    if (!token) throw new Error("Không lấy được FCM token.");

    await saveFcmTokenToFirestore(token);
    feedback.className = "text-xs text-emerald-400";
    feedback.textContent = "✅ Đã đăng ký thiết bị thành công! Bot sẽ bắn thông báo trực tiếp tới máy này.";
    await loadFcmSettings();
  } catch (err) {
    feedback.className = "text-xs text-rose-400";
    feedback.textContent = "Lỗi đăng ký FCM: " + (err.message || err);
  } finally {
    btn.disabled = false;
    btn.textContent = "🔔 Bật / Cập nhật Push Notification trên thiết bị này";
  }
}

// -------------------- Instant On-Demand Live Price Refresh --------------------
async function fetchLivePriceForTicker(ticker) {
  const sym = ticker.toUpperCase();
  const nowSec = Math.floor(Date.now() / 1000);
  const fromSec = nowSec - 10 * 86400;

  // Source 1: VNDirect dchart history (latest close/intraday bar, CORS-friendly)
  try {
    const url = `https://dchart-api.vndirect.com.vn/dchart/history?resolution=D&symbol=${encodeURIComponent(sym)}&from=${fromSec}&to=${nowSec}`;
    const resp = await fetch(url);
    if (resp.ok) {
      const data = await resp.json();
      if (data && Array.isArray(data.c) && data.c.length > 0) {
        let p = Number(data.c[data.c.length - 1]);
        if (p > 0 && p < 1000) p = Math.round(p * 1000);
        if (p > 0) return Math.round(p);
      }
    }
  } catch (err) {
    console.warn("dchart fallback error for", sym, err);
  }

  // Source 2: VNDirect finfo stock_prices
  try {
    const url = `https://finfo-api.vndirect.com.vn/v4/stock_prices?sort=date&q=code:${encodeURIComponent(sym)}&size=1`;
    const resp = await fetch(url);
    if (resp.ok) {
      const data = await resp.json();
      const row = data?.data?.[0];
      if (row) {
        let p = Number(row.close || row.adClose || 0);
        if (p > 0 && p < 1000) p = Math.round(p * 1000);
        if (p > 0) return Math.round(p);
      }
    }
  } catch (err) {
    console.warn("finfo fallback error for", sym, err);
  }

  return null;
}

async function handleFetchInstantPrices() {
  const btn = $("btn-live-price");
  const holdings = portfolioItems.filter((p) => (p.status || "holding") === "holding");
  if (holdings.length === 0) {
    showToast("Chưa có mã nào đang nắm giữ trong Danh mục.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "⏳ Đang lấy giá...";
  let updatedCount = 0;

  try {
    const nowLabel = new Date().toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
    await Promise.all(
      holdings.map(async (item) => {
        const livePrice = await fetchLivePriceForTicker(item.ticker);
        if (!livePrice) return;
        const buyPrice = Number(item.buy_price || 0);
        const oldHigh = Number(item.highest_price || buyPrice);
        const pnlPct = buyPrice > 0 ? Number((((livePrice - buyPrice) / buyPrice) * 100).toFixed(2)) : 0;
        const updates = {
          latest_price: livePrice,
          latest_pnl_pct: pnlPct,
          last_checked: `${todayIso()} ${nowLabel}`,
        };
        if (livePrice > oldHigh) updates.highest_price = livePrice;
        await setDoc(doc(db, "portfolio", item.ticker), updates, { merge: true });
        updatedCount += 1;
      })
    );

    if (updatedCount > 0) {
      await loadPortfolio();
      renderAccountSummary();
      renderPortfolio();
      showToast(`✅ Đã cập nhật giá trực tiếp cho ${updatedCount}/${holdings.length} mã!`);
    } else {
      showToast("⚠️ Không kéo được giá trực tiếp (mạng/CORS). Hãy dùng dữ liệu từ Bot 30 phút/lần.");
    }
  } catch (err) {
    showToast("Lỗi lấy giá: " + (err.message || err));
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ Giá ngay";
  }
}

// -------------------- On-Demand Potential Stock Rescan (GitHub Actions Dispatch) --------------------
const GH_TOKEN_STORAGE_KEY = "swingbot_gh_actions_token";

function loadGhTokenStatus() {
  const token = localStorage.getItem(GH_TOKEN_STORAGE_KEY) || "";
  const statusEl = $("gh-token-status");
  if (!statusEl) return;
  if (token) {
    statusEl.textContent = "✅ Đã lưu token cục bộ trên máy này. Bạn có thể bấm '🔍 Quét tiềm năng' bất cứ lúc nào.";
    statusEl.className = "text-[11px] text-emerald-400";
  } else {
    statusEl.textContent = "Chưa lưu token trên máy này.";
    statusEl.className = "text-[11px] text-slate-400";
  }
}

function handleSaveGhToken(e) {
  e.preventDefault();
  const val = $("set-gh-token").value.trim();
  if (!val) {
    localStorage.removeItem(GH_TOKEN_STORAGE_KEY);
    showToast("Đã xóa GitHub Token khỏi máy này.");
  } else {
    localStorage.setItem(GH_TOKEN_STORAGE_KEY, val);
    $("set-gh-token").value = "";
    showToast("✅ Đã lưu GitHub Token cục bộ! Bạn có thể bấm '🔍 Quét tiềm năng' ngay.");
  }
  loadGhTokenStatus();
}

async function handleTriggerRescanCandidates() {
  const token = localStorage.getItem(GH_TOKEN_STORAGE_KEY) || "";
  if (!token) {
    showToast("Vào Tab Cài đặt → dán GitHub Token (quyền Actions: Write) để bật nút Quét tiềm năng ngay!", 6000);
    // Switch user smoothly to Settings tab
    document.querySelector('.tab-btn[data-tab="settings"]')?.click();
    return;
  }

  const btn = $("btn-rescan-candidates");
  btn.disabled = true;
  btn.textContent = "⏳ Đang quét...";

  try {
    const resp = await fetch(
      "https://api.github.com/repos/thkcnpt-pixel/bottrader/actions/workflows/daily_scan.yml/dispatches",
      {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (resp.status === 204 || resp.ok) {
      showToast("🚀 Bot đang quét mã tiềm năng + Hội đồng 3 AI phản biện (~35 giây)...", 6000);
      setTimeout(async () => {
        await refreshAllData();
        showToast("✅ Đã đồng bộ báo cáo mã tiềm năng mới nhất!");
      }, 38000);
    } else {
      const errText = await resp.text();
      showToast(`Lỗi kích hoạt quét (${resp.status}): kiểm tra lại quyền Actions: Write của Token.`, 6000);
      console.warn("Workflow dispatch error:", resp.status, errText);
    }
  } catch (err) {
    showToast("Lỗi kết nối GitHub API: " + (err.message || err));
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "🔍 Quét tiềm năng";
    }, 5000);
  }
}

init();
