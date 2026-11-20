# HƯỚNG DẪN CẤU HÌNH & TRIỂN KHAI (0-VND STACK)

Tài liệu này hướng dẫn chi tiết cách điền key Firebase, Gemini API và bật GitHub Actions / GitHub Pages để hệ thống chạy tự động hoàn toàn miễn phí.

---

## BƯỚC 1: TẠO PROJECT FIREBASE (SPARK PLAN — 0 VNĐ)

1. Truy cập [Firebase Console](https://console.firebase.google.com/) → **Add project** → đặt tên (ví dụ `vn-swing-bot`) → tạo project (không cần bật Google Analytics).
2. **Bật Firestore Database:**
   - Menu trái → **Build → Firestore Database → Create database**.
   - Chọn region `asia-southeast1` (Singapore — gần Việt Nam nhất) → **Start in production mode**.
3. **Bật Authentication (Email/Password cá nhân):**
   - Menu trái → **Build → Authentication → Get started**.
   - Tab **Sign-in method** → bật **Email/Password**.
   - Tab **Users → Add user** → tạo 1 tài khoản email & mật khẩu duy nhất cho riêng bạn (ví dụ: `tenban@gmail.com`).
4. **Áp dụng Firestore Security Rules:**
   - Mở file [`firestore.rules`](./firestore.rules) trong repo này, đổi `YOUR_OWNER_EMAIL@gmail.com` thành email bạn vừa tạo ở bước 3.
   - Vào **Firestore Database → Rules**, dán toàn bộ nội dung vào và bấm **Publish**.

---

## BƯỚC 2: LẤY 2 KEY BÍ MẬT CHO GITHUB ACTIONS

### 2.1. `FIREBASE_SERVICE_ACCOUNT_JSON`
1. Trong Firebase Console → bánh răng **Project settings → Service accounts**.
2. Chọn **Python** → bấm **Generate new private key** → tải file `.json` về máy.
3. Mở file `.json` bằng trình soạn thảo, copy **toàn bộ nội dung JSON** (có thể để nguyên hoặc nén thành 1 dòng).

### 2.2. `GEMINI_API_KEY` (Google AI Studio Free Tier)
1. Truy cập [Google AI Studio](https://aistudio.google.com/apikey).
2. Bấm **Create API key** → copy chuỗi API key.

### 2.3. Thêm GitHub Repository Secrets
Vào repo GitHub của bạn → **Settings → Secrets and variables → Actions → New repository secret**, thêm chính xác **2 secrets** sau:

| Tên Secret (chính xác) | Giá trị |
| :--- | :--- |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Toàn bộ nội dung file JSON Service Account ở bước 2.1 |
| `GEMINI_API_KEY` | Chuỗi API Key từ Google AI Studio ở bước 2.2 |

---

## BƯỚC 3: CẤU HÌNH WEB APP PWA & PUSH NOTIFICATION (FCM)

1. Trong Firebase Console → **Project settings → General → Your apps** → bấm biểu tượng Web `</>` → đặt tên `SwingBot PWA` → **Register app**.
2. Copy object `firebaseConfig` (`apiKey`, `authDomain`, `projectId`, `storageBucket`, `messagingSenderId`, `appId`).
3. Vẫn trong **Project settings**, chuyển sang tab **Cloud Messaging** → mục **Web configuration → Web Push certificates** → bấm **Generate key pair** → copy **Key pair (VAPID Key)**.
4. Dán các giá trị trên vào **2 file** trong thư mục `frontend/`:
   - [`frontend/firebase-config.js`](./frontend/firebase-config.js): điền `FIREBASE_CONFIG` và `FCM_VAPID_KEY`.
   - [`frontend/firebase-messaging-sw.js`](./frontend/firebase-messaging-sw.js): điền `SW_FIREBASE_CONFIG` giống hệt `FIREBASE_CONFIG`.
5. Commit & push lên nhánh `main`.

---

## BƯỚC 4: BẬT GITHUB PAGES CHO PWA

1. Vào repo GitHub → **Settings → Pages**.
2. Ở mục **Build and deployment → Source**, chọn **GitHub Actions**.
3. Workflow `.github/workflows/deploy_pages.yml` sẽ tự động deploy thư mục `frontend/` lên địa chỉ:
   `https://<github-username>.github.io/<repo-name>/`
4. Mở link đó trên điện thoại:
   - **Android (Chrome):** Menu `⋮` → **Thêm vào Màn hình chính (Install app)**.
   - **iOS (Safari):** Nút Share → **Thêm vào Màn hình chính** → mở app từ màn hình chính → vào tab **Cài đặt** → bấm **Bật Push Notification**.

---

## BƯỚC 5: CHẠY THỬ NGAY LẦN ĐẦU (MANUAL TRIGGER)

Trước khi chờ lịch chạy tự động, hãy kích hoạt thủ công theo thứ tự:
1. Vào tab **Actions** trên GitHub → chọn **`Tier 1: Update Weekly Fundamentals (HOSE & HNX)`** → **Run workflow** (có thể nhập `limit = 30` để test nhanh, sau đó chạy `0` cho toàn sàn).
2. Sau khi Tầng 1 xong, chọn **`Tier 2-4: Daily EOD Scan, Portfolio Check & Gemini Critique`** → **Run workflow**.
3. Mở PWA → đăng nhập bằng email/password bước 1 → kiểm tra **Tab Báo cáo ngày**, thêm mã đang giữ vào **Tab Danh mục**, và bật thông báo ở **Tab Cài đặt**.

---

## LỊCH CHẠY TỰ ĐỘNG (MÚI GIỜ VIỆT NAM: UTC+7)

| Workflow | File | Lịch chạy (UTC+7) | Cron (UTC) |
| :--- | :--- | :--- | :--- |
| Tầng 1 — Quét BCTC | `update_fundamentals.yml` | Chủ nhật 20:00 | `0 13 * * 0` |
| Tầng 2–4 — Báo cáo ngày | `daily_scan.yml` | Thứ 2 – Thứ 6, 18:00 | `0 11 * * 1-5` |
| Tầng 3 — Cảnh báo trong phiên | `intraday_alert.yml` | T2–T6 mỗi 30 phút (`09:15–11:15` & `13:15–14:45`) | `15,45 2-3 * * 1-5`, `15 4 * * 1-5`, `15,45 6-7 * * 1-5` |

---

## CHẠY THỬ LOCAL (TÙY CHỌN)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Điền biến môi trường hoặc đặt DRY_RUN=true để chạy thử không ghi Firestore
DRY_RUN=true python scripts/update_fundamentals.py --limit 10
```
