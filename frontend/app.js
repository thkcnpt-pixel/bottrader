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
  return Number(val).toLocaleString("vi-VN") + " đ";
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
let portfolioItems = [];
let currentPfFilter = "holding";

// -------------------- Bootstrap --------------------
async function init() {
  if ("serviceWorker" in navigator) {
    try {
      swReg = await navigator.serviceWorker.register("./firebase-messaging-sw.js");
    } catch (err) {
      console.warn("SW registration failed:", err);
    }
  }

  if (!FIREBASE_CONFIG.apiKey || FIREBASE_CONFIG.apiKey.startsWith("REPLACE_")) {
    $("config-warning").classList.remove("hidden");
    return;
  }

  const app = initializeApp(FIREBASE_CONFIG);
  auth = getAuth(app);
  db = getFirestore(app);

  try {
    if (await isMessagingSupported()) {
      messaging = getMessaging(app);
      onMessage(messaging, (payload) => {
        const title = payload.notification?.title || "SwingBot";
        const body = payload.notification?.body || "";
        showToast(`${title}: ${body}`, 6000);
      });
    }
  } catch (err) {
    console.warn("FCM messaging unavailable:", err);
  }

  bindEvents();

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
  // Login
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

  // Logout
  $("btn-logout").addEventListener("click", async () => {
    await signOut(auth);
  });

  // Refresh
  $("btn-refresh").addEventListener("click", async () => {
    showToast("Đang đồng bộ dữ liệu từ Firestore...", 2000);
    await refreshAllData();
  });

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

  // Report date selector
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

  // Modal open/close
  $("btn-open-add-modal").addEventListener("click", () => openPortfolioModal(null));
  $("btn-close-modal").addEventListener("click", closePortfolioModal);
  $("portfolio-modal").addEventListener("click", (e) => {
    if (e.target === $("portfolio-modal")) closePortfolioModal();
  });

  // Portfolio form submit
  $("portfolio-form").addEventListener("submit", handleSavePortfolio);

  // Enable Push Notifications (FCM)
  $("btn-enable-fcm").addEventListener("click", handleRegisterFcmToken);
}

// -------------------- Data Loading --------------------
async function refreshAllData() {
  await Promise.all([loadDailyReports(), loadPortfolio(), loadFcmSettings()]);
}

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
      select.innerHTML = `<option value="">Chưa có báo cáo</option>`;
      renderEmptyReport();
      return;
    }

    select.innerHTML = dates
      .map((d) => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`)
      .join("");
    renderDailyReport(reportsCache[dates[0]]);
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

  // Portfolio alerts
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

  // Buy candidates
  const candidates = report.buy_candidates || [];
  $("candidates-count").textContent = `${candidates.length} mã`;
  const listEl = $("buy-candidates-list");

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
      const signalBadge =
        c.signal_type === "Pullback"
          ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">[Pullback]</span>`
          : `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-sky-500/20 text-sky-300 border border-sky-500/40">[Breakout]</span>`;
      const fitBadge = c.fit_500k
        ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-violet-500/20 text-violet-300 border border-violet-500/40">[Hợp vốn 500k]</span>`
        : "";
      const missBadge = c.missing_bctc
        ? `<span class="px-2 py-0.5 rounded-md text-[11px] font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/40">[Thiếu BCTC mới nhất]</span>`
        : "";

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
              <span>🤖 Phản biện rủi ro từ Gemini AI (4 tiêu chí)</span>
              <span class="text-slate-400 group-open:rotate-180 transition">▾</span>
            </summary>
            <div class="mt-3 space-y-2.5 text-xs border-t border-slate-800 pt-3">
              <div>
                <p class="font-semibold text-emerald-300">1. Chất lượng lợi nhuận (Core vs Đột biến)</p>
                <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.profit_quality || "Chưa có dữ liệu.")}</p>
              </div>
              <div>
                <p class="font-semibold text-rose-300">2. Tin xấu / Kiện tụng / Lãnh đạo bán ra</p>
                <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.negative_news || "Chưa có dữ liệu.")}</p>
              </div>
              <div>
                <p class="font-semibold text-amber-300">3. Bẫy cổ tức cổ phiếu / Phát hành thêm (Lô lẻ)</p>
                <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.dividend_trap || "Chưa có dữ liệu.")}</p>
              </div>
              <div>
                <p class="font-semibold text-sky-300">4. Rủi ro lớn nhất 1–3 tháng tới</p>
                <p class="text-slate-300 mt-0.5 leading-relaxed">${escapeHtml(g.biggest_risk || "Chưa có dữ liệu.")}</p>
              </div>
            </div>
          </details>
        </article>`;
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
    renderPortfolio();
  } catch (err) {
    console.error("Failed to load portfolio:", err);
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
      const pnlPct = buyPrice > 0 ? ((latestPrice - buyPrice) / buyPrice) * 100 : 0;
      const pnlVnd = (latestPrice - buyPrice) * qty;
      const trailingStopPrice = Math.round(highestPrice * 0.92);
      const belowMa50 = ma50 > 0 && latestPrice < ma50;

      return `
        <div class="glass-card rounded-2xl p-4 space-y-3">
          <div class="flex items-start justify-between">
            <div>
              <div class="flex items-center gap-2">
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
              <p class="text-[11px] text-slate-400 mt-0.5">
                SL: ${qty} CP • Mua: ${fmtVnd(buyPrice)} (${escapeHtml(p.buy_date || "--")})
              </p>
            </div>
            <div class="text-right">
              <p class="text-sm font-bold tabular-nums ${
                pnlPct >= 0 ? "text-emerald-400" : "text-rose-400"
              }">${pnlPct >= 0 ? "+" : ""}${fmtNum(pnlPct, 2)}%</p>
              <p class="text-[11px] tabular-nums ${
                pnlVnd >= 0 ? "text-emerald-300/80" : "text-rose-300/80"
              }">${pnlVnd >= 0 ? "+" : ""}${fmtVnd(Math.round(pnlVnd))}</p>
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
                ? `<button data-action="toggle-half" data-ticker="${escapeHtml(p.ticker)}"
                     class="px-3 py-1.5 rounded-lg text-[11px] font-semibold ${
                       p.sold_half
                         ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                         : "bg-slate-800 hover:bg-slate-700 text-slate-200"
                     }">
                     ${p.sold_half ? "✓ Đã chốt 1/2" : "Đánh dấu chốt 1/2"}
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

  // Attach card action handlers
  listEl.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      const ticker = btn.dataset.ticker;
      const item = portfolioItems.find((x) => x.ticker === ticker);
      if (!item) return;

      if (action === "edit") {
        openPortfolioModal(item);
      } else if (action === "toggle-half") {
        await setDoc(
          doc(db, "portfolio", ticker),
          { sold_half: !item.sold_half },
          { merge: true }
        );
        showToast(`Đã cập nhật trạng thái chốt 1/2 cho ${ticker}.`);
        await loadPortfolio();
      } else if (action === "delete") {
        if (confirm(`Xóa mã ${ticker} khỏi danh mục Firestore?`)) {
          await deleteDoc(doc(db, "portfolio", ticker));
          showToast(`Đã xóa ${ticker}.`);
          await loadPortfolio();
        }
      }
    });
  });
}

function openPortfolioModal(item) {
  const isEdit = Boolean(item);
  $("modal-title").textContent = isEdit ? `Cập nhật ${item.ticker}` : "Nhập lệnh Mua / Bán";
  $("pf-ticker").value = item ? item.ticker : "";
  $("pf-ticker").disabled = isEdit;
  $("pf-exchange").value = item?.exchange || "HOSE";
  $("pf-buy-price").value = item?.buy_price || "";
  $("pf-quantity").value = item?.quantity || "";
  $("pf-buy-date").value = item?.buy_date || new Date().toISOString().slice(0, 10);
  $("pf-status").value = item?.status || "holding";
  $("pf-sold-half").checked = Boolean(item?.sold_half);
  $("pf-notes").value = item?.notes || "";
  $("portfolio-modal").classList.remove("hidden");
}

function closePortfolioModal() {
  $("portfolio-modal").classList.add("hidden");
}

async function handleSavePortfolio(e) {
  e.preventDefault();
  const ticker = $("pf-ticker").value.trim().toUpperCase();
  if (!ticker) return;

  const buyPrice = Number($("pf-buy-price").value);
  const existing = portfolioItems.find((x) => x.ticker === ticker);
  const highestPrice = Math.max(Number(existing?.highest_price || 0), buyPrice);

  const payload = {
    ticker,
    exchange: $("pf-exchange").value,
    buy_price: buyPrice,
    quantity: Number($("pf-quantity").value),
    buy_date: $("pf-buy-date").value,
    highest_price: highestPrice,
    sold_half: $("pf-sold-half").checked,
    status: $("pf-status").value,
    notes: $("pf-notes").value.trim(),
    updated_at: new Date().toISOString(),
  };

  try {
    await setDoc(doc(db, "portfolio", ticker), payload, { merge: true });
    closePortfolioModal();
    showToast(`Đã lưu lệnh ${ticker} vào Firestore!`);
    await loadPortfolio();
  } catch (err) {
    alert("Lỗi lưu Firestore: " + (err.message || err.code));
  }
}

// -------------------- FCM Push Notifications --------------------
async function loadFcmSettings() {
  const badge = $("fcm-status-badge");
  if (!("Notification" in window)) {
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

async function handleRegisterFcmToken() {
  const feedback = $("fcm-feedback");
  feedback.classList.remove("hidden");

  if (!messaging) {
    feedback.className = "text-xs text-rose-400";
    feedback.textContent = "Trình duyệt này không hỗ trợ Firebase Messaging (trên iOS, hãy 'Thêm vào Màn hình chính' trước).";
    return;
  }
  if (!FCM_VAPID_KEY || FCM_VAPID_KEY.startsWith("REPLACE_")) {
    feedback.className = "text-xs text-amber-300";
    feedback.textContent = "Chưa điền FCM_VAPID_KEY trong frontend/firebase-config.js.";
    return;
  }

  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      feedback.className = "text-xs text-rose-400";
      feedback.textContent = "Bạn chưa cấp quyền hiển thị thông báo.";
      return;
    }

    const token = await getToken(messaging, {
      vapidKey: FCM_VAPID_KEY,
      serviceWorkerRegistration: swReg,
    });
    if (!token) throw new Error("Không lấy được FCM token.");

    await setDoc(
      doc(db, "settings", "fcm_tokens"),
      {
        tokens: arrayUnion(token),
        updated_at: new Date().toISOString(),
      },
      { merge: true }
    );

    feedback.className = "text-xs text-emerald-400";
    feedback.textContent = "✅ Đã đăng ký thiết bị thành công! Bot sẽ bắn thông báo trực tiếp tới máy này.";
    await loadFcmSettings();
  } catch (err) {
    feedback.className = "text-xs text-rose-400";
    feedback.textContent = "Lỗi đăng ký FCM: " + (err.message || err);
  }
}

init();
