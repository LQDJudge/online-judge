[TOC]

---

Trang này mô tả các định dạng cuộc thi được hỗ trợ bởi LQDOJ. Mỗi định dạng cuộc thi thực hiện các quy tắc chấm điểm và xử lý bài nộp khác nhau.

## 1. Default (Mặc định)

Định dạng cuộc thi mặc định. Đây là định dạng được sử dụng phổ biến nhất cho hầu hết các cuộc thi.

**Chấm điểm:** Bài nộp tốt nhất cho mỗi bài toán được sử dụng để chấm điểm. Tổng điểm là tổng điểm từ tất cả các bài toán.

**Thời gian:** Thời gian tích lũy được tính là tổng thời gian nộp bài cho tất cả các bài toán có điểm.

**Tiêu chí phụ:** Tổng thời gian tích lũy (thấp hơn là tốt hơn).

**Cấu hình:** Không có tùy chọn cấu hình.

## 2. ICPC

Định dạng ICPC (International Collegiate Programming Contest) tuân theo quy tắc ACM-ICPC.

**Chấm điểm:** Bài toán được chấm điểm là giải được (điểm đầy đủ) hoặc không giải được (0 điểm). Tổng điểm là số bài toán đã giải được.

**Thời gian:** Thời gian tích lũy là tổng thời gian nộp bài cho các bài toán đã giải được, cộng thời gian phạt.

**Phạt:** Mỗi lần nộp sai trước lần nộp đúng đầu tiên sẽ thêm thời gian phạt có thể cấu hình (mặc định: 20 phút).

**Tiêu chí phụ:** Tổng thời gian tích lũy bao gồm phạt (thấp hơn là tốt hơn).

**Cấu hình:**
- `penalty`: Số phút phạt cho mỗi lần nộp sai (mặc định: 20)

## 3. IOI

Định dạng IOI (International Olympiad in Informatics).

**Chấm điểm:** Bài nộp tốt nhất cho mỗi bài toán được sử dụng. Hỗ trợ chấm điểm từng phần.

**Thời gian:** Phạt thời gian là tùy chọn và được tắt theo mặc định.

**Tiêu chí phụ:** Không có (cho phép hòa).

**Cấu hình:**
- `cumtime`: Đặt thành `true` để bật phạt thời gian (mặc định: `false`)

## 4. New IOI (IOI Mới)

Định dạng IOI nâng cao với hỗ trợ subtask ẩn, được giới thiệu trong IOI 2016.

**Chấm điểm:** Tương tự định dạng IOI nhưng với chấm điểm dựa trên subtask. Một số subtask có thể được ẩn trong cuộc thi.

**Subtask ẩn:** Hỗ trợ ẩn các subtask cụ thể khỏi thí sinh trong cuộc thi. Kết quả cho subtask ẩn chỉ được tiết lộ sau khi cuộc thi kết thúc.

**Thời gian:** Phạt thời gian là tùy chọn và được tắt theo mặc định.

**Cấu hình:**
- `cumtime`: Đặt thành `true` để bật phạt thời gian (mặc định: `false`)

## 5. AtCoder

Định dạng cuộc thi AtCoder, tuân theo quy tắc chấm điểm của AtCoder.

**Chấm điểm:** Bài nộp tốt nhất cho mỗi bài toán được sử dụng để chấm điểm.

**Thời gian:** Sử dụng thời gian nộp bài tối đa trong số tất cả các bài toán đã giải (không tích lũy).

**Phạt:** Mỗi lần nộp sai thêm thời gian phạt có thể cấu hình (mặc định: 5 phút).

**Tiêu chí phụ:** Tổng thời gian bao gồm phạt (thấp hơn là tốt hơn).

**Cấu hình:**
- `penalty`: Số phút phạt cho mỗi lần nộp sai (mặc định: 5)

## 6. ECOO

Định dạng ECOO (Educational Computing Organization of Ontario) với chấm điểm thưởng.

**Chấm điểm:** Sử dụng lần nộp cuối cùng cho mỗi bài toán. Bao gồm điểm thưởng cho giải pháp đúng lần đầu và thưởng thời gian.

**Thưởng:**
- **Thưởng AC đầu tiên:** Điểm thêm được trao cho việc giải bài toán trong lần nộp đầu tiên không phải IE/CE
- **Thưởng thời gian:** Điểm thêm dựa trên việc nộp bài sớm

**Thời gian:** Thời gian tích lũy là tùy chọn.

**Cấu hình:**
- `cumtime`: Đặt thành `true` để sử dụng thời gian tích lũy cho tiêu chí phụ (mặc định: `false`)
- `first_ac_bonus`: Điểm được trao cho giải pháp AC lần đầu (mặc định: 10)
- `time_bonus`: Phút trên mỗi điểm thưởng cho nộp bài sớm (mặc định: 5, đặt thành 0 để tắt)

## 7. Ultimate

Định dạng đơn giản chỉ xem xét lần nộp cuối cùng cho mỗi bài toán.

**Chấm điểm:** Chỉ lần nộp gần nhất cho mỗi bài toán được xem xét, bất kể điểm số của nó.

**Thời gian:** Phạt thời gian là tùy chọn và được tắt theo mặc định.

**Trường hợp sử dụng:** Phù hợp cho các cuộc thi mà thí sinh nên được khuyến khích tiếp tục cải thiện giải pháp của họ.

**Cấu hình:**
- `cumtime`: Đặt thành `true` để bật phạt thời gian (mặc định: `false`)

---

## Chọn định dạng

- **Default:** Tốt nhất cho hầu hết các cuộc thi, mục đích giáo dục, và khi bạn muốn điểm cao nhất được tính
- **ICPC:** Cho các cuộc thi kiểu ACM-ICPC nơi bài toán được giải hoặc không được giải
- **IOI:** Cho các cuộc thi kiểu olympiad với chấm điểm từng phần
- **New IOI:** Cho các cuộc thi olympiad nâng cao với subtask ẩn
- **AtCoder:** Cho các cuộc thi tuân theo hệ thống phạt của AtCoder
- **ECOO:** Cho các cuộc thi với hệ thống chấm điểm thưởng
- **Ultimate:** Cho các cuộc thi nơi chỉ lần nộp cuối cùng quan trọng

## Định dạng cấu hình

Cấu hình định dạng cuộc thi được chỉ định dưới dạng đối tượng JSON. Ví dụ:

```json
{
  "penalty": 20,
  "cumtime": true
}
```

Nếu không cần cấu hình, để trống trường hoặc sử dụng đối tượng rỗng `{}`.
