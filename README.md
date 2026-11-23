# FinAgent AI — Hệ Thống AI Agent Tự Động Hoá Kế Toán (Việt Nam)

Hệ thống tự động hoá nghiệp vụ kế toán đầu vào/đầu ra theo chuẩn **Thông tư 78/2021/TT-BTC**, **Thông tư 200 / 133**, đối soát 2–3 chiều và xuất nhập khẩu đa phần mềm kế toán (**MISA**, **Fast**, **Odoo**).

## Tính năng chính
1. **Đọc chứng từ Hybrid (XML-first):** Parse thẳng file `.xml` Thông tư 78 (độ chính xác 100%, chi phí 0đ), hỗ trợ đa thuế suất (`0%`, `5%`, `8%`, `10%`, `KCT`, `KKKNT`), chiết khấu thương mại, ngoại tệ & tỷ giá, hoá đơn thay thế/điều chỉnh; tự động fallback sang PDF text hoặc **Gemini 2.5 Flash Vision** cho ảnh chụp/PDF scan.
2. **Deterministic Rule Engine (Chống ảo giác):** Kiểm tra công thức toán học từng dòng & tổng hoá đơn, checksum MST Việt Nam (Mod-11), tỷ giá ngoại tệ, chữ ký số `XMLDSig` và phát hiện hoá đơn trùng lặp.
3. **Accounting Coding Agent + Feedback Loop:** Gợi ý bút toán Nợ/Có theo **TT200 / TT133** (tự quy đổi ngoại tệ sang VNĐ) và tự học quy tắc định khoản theo từng nhà cung cấp sau mỗi lần kế toán bấm duyệt.
4. **Đối soát 2 chiều / 3 chiều & Báo cáo trùng lặp:** So khớp Hoá đơn ↔ PO ↔ Phiếu nhập kho (GRN) ↔ Sao kê ngân hàng; báo cáo riêng các nhóm hoá đơn trùng lặp và xoá an toàn qua xác nhận 2 bước.
5. **Phân quyền RBAC, Upload hàng loạt & Email IMAP:** 3 vai trò (`Kế toán viên`, `Kế toán trưởng`, `Quản trị viên`), upload nhiều file cùng lúc, kéo tự động file đính kèm từ hòm thư hoá đơn IMAP.
6. **Xuất đa ERP & Eval Harness:** Xuất file chuẩn **MISA (`.xlsx`)**, **Fast (`.xlsx`)**, **Odoo (`.csv`)**; tích hợp bộ **Eval Harness** (`eval/run_eval.py`) chấm tự động 8 KPI nghiệm thu và dashboard telemetry live (`GET /api/metrics`).

---

## Cài đặt & Chạy nhanh (Python 3.11+)

### 1. Tải mã nguồn & cài thư viện
```bash
git clone <url-repo-cua-ban>
cd <ten-thu-muc-repo>
python -m venv .venv
```

- **Trên Windows (PowerShell):**
  ```powershell
  .\.venv\Scripts\pip.exe install -r requirements.txt
  .\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
  ```
- **Trên macOS / Linux:**
  ```bash
  source .venv/bin/activate
  pip install -r requirements.txt
  uvicorn app.main:app --reload --port 8000
  ```

Mở trình duyệt tại **http://localhost:8000** và bấm **"Nạp dữ liệu mẫu VN"** (trong menu `⋯` góc trên phải hoặc nút giữa màn hình) để trải nghiệm ngay. Xem hướng dẫn nhập thử chi tiết tại [HUONG_DAN_NHAP_THU.md](./HUONG_DAN_NHAP_THU.md).

### 2. Chạy bằng Docker (Tuỳ chọn)
```bash
docker build -t finagent-ai .
docker run -p 8000:8000 -e GEMINI_API_KEY="your-key-if-needed" finagent-ai
```

### 3. Bật đọc ảnh chụp / PDF scan bằng Gemini Vision (Tuỳ chọn)
File `.xml` Thông tư 78 và `.pdf` có lớp text chạy **100% offline không cần API key**. Để đọc ảnh chụp điện thoại / PDF scan, tạo biến môi trường `GEMINI_API_KEY` (xem `.env.example`):
- **Windows PowerShell:** `$env:GEMINI_API_KEY="AIza..."`
- **macOS / Linux:** `export GEMINI_API_KEY="AIza..."`

---

## Kiểm thử tự động & Đánh giá chất lượng (Eval)

```bash
# Chạy toàn bộ Unit & End-to-End tests
pytest -v

# Chạy Eval Harness trên Golden Dataset (xuất báo cáo eval/report.json)
python eval/run_eval.py
```
