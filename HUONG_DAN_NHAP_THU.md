# Hướng Dẫn Nhập Thử & Kiểm Thử Hệ Thống (FinAgent AI MVP)

Tài liệu này hướng dẫn từng bước để bạn tự tay nhập thử toàn bộ 6 luồng nghiệp vụ của hệ thống trong 10 phút.

---

## Bước 0: Khởi động hệ thống
Mở PowerShell và chạy:
```powershell
cd C:\Users\synvi\.gemini\antigravity\scratch\accounting-ai-agent
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```
Mở trình duyệt tại: **http://localhost:8000**

---

## Kịch bản 1: Nạp dữ liệu mẫu tự động (Xem đủ 3 trạng thái hoá đơn)
1. Bấm nút **"Nạp dữ liệu mẫu VN"** ở góc trên bên phải.
2. Quan sát cột danh sách bên trái sẽ có ngay 3 hoá đơn XML Thông tư 78:
   - **`C26THH #00001234`** (`MATCHED`): Hoá đơn hợp lệ có 3 dòng hàng với **3 mức thuế suất khác nhau (`8%`, `10%`, `0%`)**, có chữ ký số, khớp 100% với Đơn đặt hàng `PO-2026-0088`, Phiếu nhập kho `PNK-2026-0142` và Sao kê ngân hàng `VCB-FT2628800192`.
   - **`C26TVC #00005599`** (`VALIDATION_FAILED`): Hoá đơn bị **lỗi toán học** (`10 × 250.000 = 2.500.000` nhưng hoá đơn ghi `2.400.000`, tổng tiền ghi `2.692.000`). Rule Engine lập tức chặn bằng banner đỏ.
   - **`C26TVC #00005600`** (`NEEDS_REVIEW`): Hoá đơn **thay thế** cho số `00005599`. Hệ thống hiện cảnh báo vàng để kế toán kiểm tra tránh hạch toán trùng.

---

## Kịch bản 2: Thử sửa lỗi toán học & kiểm chứng Rule Engine
1. Chọn hoá đơn **`C26TVC #00005599`** (đang màu đỏ `VALIDATION_FAILED`).
2. Ở cột phải, đổi **Tổng thanh toán** thành `2592000` rồi bấm **"Lưu sửa đổi"** → cảnh báo `GRAND_TOTAL_MISMATCH` biến mất ngay, chỉ còn `LINE_MATH_MISMATCH` (do dòng hàng vẫn lệch `10 × 250.000 != 2.400.000`).
3. Bấm **"Từ chối (Alt+R)"** để loại bỏ hoá đơn sai này (vì đã có hoá đơn thay thế `00005600`).

---

## Kịch bản 3: Thử đối soát 2 chiều / 3 chiều (Tự nhập PO & Sao kê ngân hàng)
1. Chọn hoá đơn **`C26TVC #00005600`** (Tổng tiền `2.700.000 VNĐ`, MST `0300588569`).
2. Bấm nút **"+ Thêm PO / Sao kê"** trên thanh công cụ (hệ thống tự điền sẵn MST `0300588569` và số tiền `2700000`).
3. Nhập thử 2 trường hợp để thấy sự khác biệt:
   - **Trường hợp khớp (`MATCHED`):** Loại `Đơn đặt hàng (PO)`, Mã `PO-2026-0100`, Tổng tiền `2700000` → Bấm **"Lưu & Đối soát lại"** → chuyển sang tab **"Đối soát 2/3 chiều"** sẽ thấy nhãn xanh `MATCHED` (chênh lệch `0 VNĐ`).
   - **Trường hợp lệch (`FLAGGED`):** Bấm **"+ Thêm PO / Sao kê"** lần nữa → Loại `Sao kê ngân hàng`, Mã `VCB-999888`, Tổng tiền `2500000` (lệch 200.000 VNĐ) → Lưu → hệ thống lập tức gắn cờ đỏ `FLAGGED` kèm số tiền lệch `200.000 VNĐ`.

---

## Kịch bản 4: Thử Feedback Loop (Hệ thống tự học tài khoản định khoản)
1. Chọn hoá đơn **`C26THH #00001234`** (Văn phòng phẩm Hồng Hà).
2. Ở bảng **Dòng hàng & Bút toán định khoản**, sửa ô **TK Nợ** dòng 1 thành `6428` và gõ **Mã hàng** là `VPP-A4-01`.
3. Tích chọn **"Ghi nhớ tài khoản định khoản làm mặc định cho NCC 0100109106"**.
4. Bấm **"Duyệt & Tự học (Alt+A)"** → hệ thống báo đã học 3 quy tắc định khoản và ghi lại lịch sử trong tab **Audit Trail**.
5. *Kiểm chứng tự học:* Tải lên lại file `samples\01_hd_gtgt_hop_le_da_thue_suat.xml` (hoặc tạo 1 bản copy đổi thẻ `<SHDon>00001235</SHDon>`):
   - Nếu giữ nguyên số `00001234` → hệ thống lập tức gắn cờ đỏ **`FLAGGED` (`DUPLICATE_INVOICE`)**.
   - Nếu đổi số thành `00001235` → hệ thống tự động điền sẵn **TK Nợ `6428`** và **Mã hàng `VPP-A4-01`** với lý do *"Khớp lịch sử đã duyệt 1 lần của NCC này"*.

---

## Kịch bản 5: Tải lên file hoá đơn thật của doanh nghiệp bạn
- **File `.xml` Thông tư 78:** Kéo-thả trực tiếp vào khung nét đứt bên trái (không cần API key, chạy 100% offline, độ chính xác 100%).
- **File `.pdf` / ảnh chụp (`.jpg`, `.png`):**
  1. Nếu PDF có lớp text: kéo-thả trực tiếp sẽ chạy bộ đọc offline (độ tin cậy ~72%, tự đẩy vào `NEEDS_REVIEW`).
  2. Để đọc chính xác ảnh chụp mờ/PDF scan bằng **Gemini 2.5 Flash Vision**, đặt biến môi trường trước khi chạy server:
     ```powershell
     $env:GEMINI_API_KEY="AIza..."
     .\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
     ```
  3. Khi mở hoá đơn ảnh, bấm vào ô **MST Người bán** hoặc **Tổng thanh toán** ở cột phải — cột trái sẽ tự vẽ khung xanh highlight đúng vùng đã đọc trên ảnh.

---

## Kịch bản 6: Xuất file Excel chuẩn nhập khẩu MISA
1. Bấm nút xanh **"Xuất Excel MISA"** ở góc trên bên phải.
2. Trình duyệt tải về file `MISA_Import_ChungTuMuaHang.xlsx` (đủ 21 cột chuẩn chứng từ mua hàng: Ngày hạch toán, Ký hiệu, Số HĐ, MST NCC, Mã hàng, TK Nợ, TK Có, % Thuế GTGT, Tiền thuế, TK Thuế `1331`...).
3. Các hoá đơn đã duyệt tự động chuyển trạng thái sang **`SYNCED`**.
