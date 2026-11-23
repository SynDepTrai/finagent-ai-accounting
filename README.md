# AI Accounting Agent MVP (Việt Nam)

Hệ thống MVP tự động hoá nghiệp vụ kế toán theo chuẩn **Thông tư 78/2021/TT-BTC**, **Thông tư 200/133** và xuất file nhập khẩu **MISA AMIS / MISA SME**.

## Tính năng đã hoàn thiện
1. **Đọc chứng từ Hybrid (XML-first):** Parse thẳng file `.xml` Thông tư 78 (độ chính xác 100%, chi phí 0đ), fallback sang PDF text hoặc **Gemini 2.5 Flash VLM** cho ảnh chụp/PDF scan.
2. **Deterministic Rule Engine (Chống ảo giác):** Kiểm tra toán học từng dòng & tổng hoá đơn, thuế suất (`0%`, `5%`, `8%`, `10%`, `KCT`), checksum MST Việt Nam (Mod-11), chữ ký số XMLDSig, hoá đơn điều chỉnh/thay thế và chống trùng lặp.
3. **Accounting Coding Agent + Feedback Loop:** Tự gợi ý bút toán Nợ/Có theo TT200/TT133 và tự học quy tắc định khoản theo từng nhà cung cấp sau mỗi lần kế toán bấm duyệt.
4. **Đối soát 2 chiều / 3 chiều:** So khớp Hoá đơn ↔ PO ↔ Phiếu nhập kho (GRN) ↔ Sao kê ngân hàng, tự gắn trạng thái `MATCHED`, `MINOR_DIFF`, `NEEDS_REVIEW`, `FLAGGED`, `VALIDATION_FAILED`, `APPROVED`.
5. **Side-by-Side Review UI & Xuất Excel MISA:** Màn hình duyệt chứng từ song song (phím tắt `Alt+A`, `Alt+F`, `Alt+R`), Audit Trail truy vết và xuất file `.xlsx` chuẩn MISA.

## Chạy nhanh trên Windows (PowerShell)
```powershell
cd C:\Users\synvi\.gemini\antigravity\scratch\accounting-ai-agent
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```
Mở trình duyệt tại `http://localhost:8000` và bấm **"Nạp dữ liệu mẫu VN"** để trải nghiệm ngay.

*(Tuỳ chọn)* Để đọc ảnh chụp/PDF scan bằng Gemini Vision:
```powershell
$env:GEMINI_API_KEY="your-key-here"
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```

## Chạy kiểm thử tự động
```powershell
.\.venv\Scripts\pytest.exe -v
```
