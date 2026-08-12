# Ghi chú vận hành solver (nội bộ đội — không nộp file này)

## Chạy hằng ngày (vòng public)

```bash
python solver.py --orders <de_public>.csv --out TEN_DOI.json --team TEN_DOI --time 60
python validate.py --orders <de_public>.csv --submission TEN_DOI.json
```

Trên Windows nếu console báo lỗi Unicode thì đặt biến môi trường `PYTHONUTF8=1` trước.

## Ngày thi — vòng private (20 bài, cổng nộp mở ~45 phút)

1. **Diễn thử trước trên bộ public, bấm giờ.** Đúng lệnh sẽ chạy thật.
2. Chạy với ngân sách an toàn, ví dụ máy 8 lõi:

   ```bash
   python solver.py --orders private.csv --out TEN_DOI.json --team TEN_DOI --time 90 --jobs auto
   ```

   `--jobs auto` = số lõi − 1. 20 bài / 7 tiến trình ≈ 3 đợt ≈ **4.5 phút** với
   `--time 90`. Solver in ước tính tổng thời gian ngay dòng đầu — nhìn nó mà chỉnh.
3. Solver **ghi file sau mỗi instance xong** — lỡ phải Ctrl+C thì file vẫn hợp lệ
   với các bài đã xong. Nhưng bài chưa chạy sẽ THIẾU (0 điểm) — đừng ngắt sớm nếu
   không bắt buộc.
4. Luôn `validate.py` lần cuối trước khi đính kèm lên Discord. Tên file = tên đội.

## Hiệu chỉnh trọng số (quan trọng nhất để leo hạng)

Ban tổ chức **giấu** công thức chi phí. Solver tối ưu theo giả thuyết:

```
cost = w_dist·km + w_vehicle·xe + w_late·phút_trễ + w_overtime·phút_ngoài_ca
       + Σ(w_drop_base + w_drop_demand·demand) trên các đơn bỏ
```

Mặc định: `w_dist=1, w_vehicle=40, w_late=2, w_overtime=3, w_drop_base=120, w_drop_demand=12`.

Cách hiệu chỉnh bằng bảng xếp hạng public (nộp lại thoải mái):

1. Nộp bản mặc định, ghi lại điểm.
2. Đổi MỘT trọng số mỗi lần (ví dụ `--w-late 4`), chạy lại, nộp, so điểm.
3. Trọng số nào tăng điểm thì giữ. Vài vòng là hình dáng công thức thật lộ ra:
   - Điểm tăng khi ép trễ về 0 → tăng `w-late` (phạt trễ thật nặng hơn ta nghĩ, có khi phi tuyến).
   - Điểm tăng khi dùng ít xe hơn → tăng `w-vehicle`.
   - Điểm tăng khi bỏ bớt đơn xa → giảm `w-drop-base`.
4. Chốt bộ trọng số tốt nhất TRƯỚC giờ phát đề private, đừng thử nghiệm lúc đó.

## Thuật toán bên trong (để ai đọc code khỏi lạc)

- **Khởi tạo:** chèn tham lam từng đơn vào vị trí làm chi phí tăng ít nhất, 3 lần
  với thứ tự khác nhau, giữ bản tốt nhất.
- **LNS (phá rồi sửa):** mỗi vòng gỡ 4–60 đơn bằng một trong 4 phép phá — ngẫu
  nhiên / đơn đắt nhất / cụm liên quan (Shaw) / nguyên tuyến — rồi chèn lại chỗ rẻ
  nhất. Phép phá nào hay ra cải thiện được ưu tiên dần (adaptive).
- **Chấp nhận:** luyện kim mô phỏng — đầu buổi chấp nhận cả bước lùi nhỏ để thoát
  cực trị địa phương, nguội dần về cuối. Lời giải tốt nhất luôn được giữ riêng.
- **Đánh bóng:** 2-opt (đảo đoạn trong tuyến), or-opt (dời cụm 1–3 đơn),
  relocate/swap giữa các tuyến qua danh sách 20 hàng xóm gần nhất.
- **Bỏ đơn:** đơn nào chèn đắt hơn tiền phạt bỏ thì để lại; mỗi vòng đều thử cứu
  lại đơn đang bỏ nếu giờ chèn được rẻ.

Kết quả đối chứng trên bộ mẫu (10s/bài): 107 / 142 / 151 km, 0 trễ, 0 bỏ —
starter là 235 / 330 / 175 km còn dính trễ. Bài tự sinh 300 đơn: 846 km, trễ 0.1
phút (starter: 1589 km, trễ 3577 phút).
