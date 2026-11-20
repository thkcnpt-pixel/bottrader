# VN Stock Swing Bot & Personal PWA (0-VND Stack)

Hệ thống bot hỗ trợ đầu tư chứng khoán Việt Nam (**HOSE & HNX**) theo phong cách **Swing / Trung hạn** (giữ vài ngày đến vài tháng), thiết kế theo kiến trúc lai **"Lõi cứng — Vỏ thông minh"** chạy hoàn toàn trên hạ tầng **0 đồng (100% Free Tier)**.

## Kiến trúc hệ thống
- **Lõi cứng (Python 3.11 + GitHub Actions):** Toàn bộ lọc thanh khoản, chấm điểm BCTC 3 bảng (4–8 quý), tính chỉ báo kỹ thuật (`MA20`, `MA50`, `RSI14`, `Vol_MA20`), quản trị danh mục và cảnh báo trong phiên chạy bằng Python thuần — đảm bảo chính xác 100%, không dùng Multi-Agent tự trị.
- **Vỏ thông minh (Gemini Flash Free Tier):** Chỉ gọi ở bước cuối cho tối đa 3 mã đạt điểm mua/ngày để phản biện rủi ro (4 tiêu chí JSON cố định, **không có quyền Veto**).
- **Dữ liệu & Thông báo:** Firebase Firestore (Spark Plan) + Firebase Cloud Messaging (FCM).
- **Giao diện cá nhân:** Web App PWA Mobile-First (`frontend/`) host miễn phí trên GitHub Pages.

## Cấu trúc thư mục
```text
├── .github/workflows/
│   ├── update_fundamentals.yml   # Chủ nhật 20:00 (UTC+7) — Quét BCTC toàn sàn
│   ├── daily_scan.yml            # T2–T6 18:00 (UTC+7) — Quét kỹ thuật + Danh mục + Gemini
│   ├── intraday_alert.yml        # T2–T6 mỗi 30 phút (09:15–11:15 & 13:15–14:45 UTC+7)
│   └── deploy_pages.yml          # Tự động deploy frontend/ lên GitHub Pages
├── scripts/
│   ├── update_fundamentals.py    # Tầng 1: Lọc cứng & Chấm điểm BCTC (0–100đ)
│   ├── daily_scan.py             # Tầng 2, 3, 4: Kỹ thuật + Danh mục + Gemini + FCM
│   └── intraday_alert.py         # Tầng 3: Cảnh báo khẩn cấp trong phiên (chống spam)
├── utils/
│   ├── config.py                 # Hằng số & cấu hình tập trung
│   ├── indicators.py             # Chỉ báo thuần Python (MA, RSI, Pullback, Breakout, Risk)
│   ├── vnstock_client.py         # Wrapper vnstock tự động fallback VCI -> TCBS
│   ├── firebase_client.py        # Firestore CRUD & FCM Push Notification
│   ├── news_scraper.py           # Cào tin vnstock + CafeF/Vietstock/VnEconomy/Google News RSS
│   └── gemini_analyst.py         # Gemini Flash API trả về JSON phản biện 4 trường
├── frontend/                     # Web App PWA (HTML5 + Tailwind + Firebase v10)
├── tests/                        # Unit tests kiểm thử lõi thuật toán
├── firestore.rules               # Security Rules chỉ cho phép chủ sở hữu đọc/ghi
├── requirements.txt
├── .env.example
└── SETUP_GUIDE.md                # Hướng dẫn cấu hình Firebase & GitHub Secrets từ A–Z
```

## Bắt đầu nhanh
Xem hướng dẫn chi tiết từng bước tại **[SETUP_GUIDE.md](./SETUP_GUIDE.md)**.

---
*Dev by Thkc*