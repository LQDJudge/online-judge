[TOC]

## 1. Bắt đầu từ đây {#start-here}

Trang Test Data chuyển file test, generator, checker và quy tắc tính điểm của bạn thành file `init.yml` để judge sử dụng. Hầu hết bài chỉ cần ba bước:

1. Thêm test, bằng cách upload ZIP ở **File zip chứa test** hoặc upload chương trình C++ ở **File sinh test**.
2. Bấm **Điền test** hoặc dùng **Thêm test mới** cho đến khi bảng **Test Cases** có một dòng thật cho mỗi test.
3. Chọn **Checker**, kiểm tra lại điểm và pretest, rồi bấm **Lưu**.

Dòng loại **Normal case** là test thật. Dòng **Batch start** và **Batch end** chỉ dùng để gom test thành subtask; hai loại dòng này không chạy như test riêng. Mỗi normal case chỉ nên dùng đúng một nguồn dữ liệu: hoặc **Input file** / **Output file** từ ZIP, hoặc **Tham số sinh test** cho generator.

Khi bấm **Lưu**, hệ thống lưu file và các dòng test, sinh `init.yml`, rồi báo cho judge biết test data của bài đã thay đổi. Nếu trang hiện feedback lỗi phía trên form, hãy sửa lỗi đó rồi bấm **Lưu** lại.

### Bản đồ giao diện {#ui-map}

Dùng bảng này để biết chính xác mỗi control trên trang Test Data dùng làm gì.

| Control trên UI | Điền gì vào đó | Khi nào dùng |
|---|---|---|
| **File zip chứa test** | File `.zip` chứa input/output riêng tư. | Bạn đã có sẵn các file kiểu `.in` / `.out`. |
| **File sinh test** | File C++, thường đặt tên `gen.cpp`. | Bạn muốn judge sinh test từ tham số thay vì lưu toàn bộ file test. |
| **Generator Script** | Mỗi dòng là một bộ tham số cho một test sinh tự động. Mở mục này từ dòng generator sau khi đã lưu generator. | Bạn có generator và muốn tạo nhiều dòng test. |
| **Checker** | Cách chấm: `standard`, `floats`, `custom`, `testlib`, `csv_*`, v.v. | Bài nào cũng cần checker; `standard` phù hợp với đa số bài exact-output. |
| **Checker arguments** | JSON tuỳ chọn cho checker đã chọn. UI có ô hỗ trợ cho checker số thực và CSV. | Cần khi cấu hình sai số, cột CSV, public/private leaderboard, hoặc tuỳ chọn riêng của checker. |
| **Custom checker file** | Source checker Python. | Dùng với **Custom checker (PY)**. |
| **Custom cpp checker file** | Source checker C++ hoặc Testlib. | Dùng với **Custom checker (CPP)**, **Testlib**, hoặc **Testlib (CMS / IOI)**. |
| **Interactive judge** | Source interactor C++. | Dùng với **Interactive** hoặc **Interactive (Testlib)**. |
| **Input file name** / **Output file name** | Tên file mà bài nộp dùng thay cho stdin/stdout. | Chỉ dùng cho bài file I/O. Để trống cho stdin/stdout bình thường. |
| **Output-only?** | Checkbox. | Người giải nộp file output thay vì source code. |
| **Binary answer data** | Checkbox. | Đáp án hoặc output nộp lên là dữ liệu nhị phân, ví dụ `.npy`, `.npz`, ảnh hoặc archive. |
| **Output submission size limit (MB)** | Dung lượng tối đa của ZIP output-only. | Bài output-only có file kết quả lớn. |
| **Nộp bài bằng hàm?** | Checkbox mở các dòng signature grader theo ngôn ngữ. | Người giải cài đặt hàm thay vì chương trình đầy đủ. |
| **Bài Communication** | Checkbox cho bài IOI kiểu manager/user nhiều tiến trình. | Gói IOI tương tác có `manager.cpp`. |
| **Manager** và **Số tiến trình** | Source manager và số tiến trình bài làm. | Chỉ dùng cho bài communication. |
| **Trình kiểm tra test** | Source validator C++ hoặc Python. | Bạn muốn kiểm tra mọi input đã upload/sinh ra có đúng ràng buộc. |
| **Tự động điền test** | Chế độ batch, vị trí bắt đầu batch, **Điền test**, và **Or use custom JSON**. | Tạo nhanh hoặc thay thế các dòng trong bảng **Test Cases**. |
| **Test Cases** | Danh sách case và batch cuối cùng mà judge dùng. | Luôn kiểm tra bảng này trước khi bấm **Lưu**. |

Bảng **Test Cases** có các cột sau:

| Cột | Ý nghĩa |
|---|---|
| **Type** | `Normal case` chạy một test. `Batch start` bắt đầu subtask. `Batch end` kết thúc subtask. |
| **Input file** | File trong ZIP dùng làm input cho judge. Để trống với dòng chỉ dùng generator. |
| **Output file** | File trong ZIP dùng làm output mong đợi. Để trống với dòng chỉ dùng generator. |
| **Points** | Với normal case là trọng số test. Với batch-start row là tổng điểm của batch. |
| **Pretest?** | Test có dùng trong chế độ chỉ chấm pretest hay không. Với test trong batch, đặt cờ này ở dòng batch-start. |
| **Tham số sinh test** | Tham số truyền vào generator cho test này. Để trống với dòng dùng file trong ZIP. |
| **Xoá?** | Xoá dòng khi bấm **Lưu**. |

## 2. Thêm test {#adding-tests}

### 2.1. Upload ZIP {#zip-test-data}

Dùng **File zip chứa test** khi bạn đã có các file `.in` / `.out`. Tên file trong cột **Input file** và **Output file** phải tồn tại trong ZIP. Sau khi upload ZIP, bấm **Điền test** để tự ghép input/output, hoặc dùng **Thêm test mới** rồi chọn file thủ công.

Hãy để ZIP này chỉ chứa dữ liệu chấm riêng tư. File trong ZIP không hiển thị cho người giải; nếu người giải cần tải input cho bài output-only hoặc Kaggle, hãy upload các file public đó ở tab **Tệp đính kèm** của trang chỉnh sửa bài.

### 2.2. Dùng Generator {#test-generator}

Dùng generator khi mô tả test bằng ràng buộc dễ hơn lưu từng file `.in` / `.out`. Generator là một chương trình C++. Chương trình nhận tham số dòng lệnh, thường gồm tham số ràng buộc và seed, rồi in:

- input của test ra **stdout**
- đáp án mong đợi ra **stderr**

```bash
./generator [arg_1] [arg_2] ... [seed]
```

Giới hạn thời gian của generator chính là giới hạn thời gian của bài, nên hãy viết như code thi lập trình bình thường. Dùng fast I/O cho `cout` và tắt tự động flush của `cerr`:

```cpp
ios::sync_with_stdio(false);
cin.tie(nullptr);
cerr.unsetf(ios::unitbuf);
```

Nên dùng `'\n'` thay vì `endl`. Điều này quan trọng vì đáp án được in ra `cerr`; mặc định `cerr` flush sau mỗi lần ghi, nên output lớn có thể làm generator chạy chậm không cần thiết.

**Ví dụ.** Bài toán: input gồm hai số `a, b` với `1 <= a, b <= 100000`. Output: `a + b`.

```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char* argv[]) {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    cerr.unsetf(ios::unitbuf);

    if (argc != 4) {
        cerr << "Usage: ./generator <x> <y> <global_seed>" << endl;
        return 1;
    }

    int x = stoi(argv[1]);
    int y = stoi(argv[2]);
    int global_seed = stoi(argv[3]);

    if (x > y) {
        cerr << "Error: x should be less than or equal to y\n";
        return 1;
    }

    int combined_seed = global_seed ^ (x * 31 + y * 37);
    mt19937 gen(combined_seed);
    uniform_int_distribution<> dist(x, y);

    int a = dist(gen);
    int b = dist(gen);
    int c = a + b;

    cout << a << ' ' << b << '\n';
    cerr << c << '\n';

    return 0;
}
```

### 2.3. Script sinh test {#generator-script}

Sau khi lưu file generator, dùng **Generator Script** để tạo nhanh nhiều test sinh tự động. Mỗi dòng không rỗng và không phải comment sẽ trở thành một normal case. Dòng được tách theo dấu cách và truyền thẳng vào generator. Dùng seed khác nhau trừ khi bạn cố ý muốn test trùng.

Với bài `a + b`, bộ 10 test này phủ các dải nhỏ, trung bình và lớn:

```
# Giá trị nhỏ
1 10 12
1 10 5123
1 10 254

# Giá trị trung bình
100 1000 51234
100 1000 4135
100 1000 123

# Giá trị lớn
10000 100000 456
10000 100000 4129
10000 100000 5912
10000 100000 4753
```

Bấm **Điền test** để tạo một dòng test cho mỗi dòng script. Tham số hiển thị ở cột **Tham số sinh test** và có thể chỉnh trực tiếp. **Thêm test mới** có thể thêm một test sinh tự động thủ công.

### 2.4. Tự điền bằng JSON {#custom-json-autofill}

Dùng **Or use custom JSON** khi các dòng cần tạo đã được xác định sẵn hoặc được sinh bởi công cụ khác. Mỗi dòng chỉ nên dùng `testcase` cho dữ liệu từ ZIP hoặc `generator_args` cho dữ liệu sinh tự động, không dùng cả hai.

Ví dụ không chia batch:

```json
[
  {"score": 1, "testcase": "1"},
  {"score": 1, "generator_args": "2 --small"},
  {"score": 2, "testcase": "complete-small-01"}
]
```

Ví dụ có batch:

```json
[
  {"score": 21, "testcases": ["subtask1-01", "subtask1-02", "subtask1-03"]},
  {"score": 79, "testcases": [
    "subtask2-01",
    {"generator_args": "2 --large"},
    "subtask2-03"
  ]}
]
```

JSON chỉ điền các dòng trên trang. Hãy kiểm tra kết quả rồi bấm **Lưu**.

### 2.5. Batch và cách tính điểm {#batches-and-scoring}

Dùng batch cho subtask. Nhập **Vị trí bắt đầu batch** là số thứ tự test đầu tiên của mỗi batch. Ví dụ `1, 5, 9` tạo các batch `[1..4]`, `[5..8]`, và `[9..hết]`.

Menu **Chế độ batch** quyết định cách **Điền test** gán điểm:

| Chế độ | Dùng khi | Kết quả được tạo |
|---|---|---|
| **Sum (VOI)** | Mỗi test đóng góp điểm độc lập | Mỗi test có trọng số `1`; điểm batch là tổng trọng số các test pass. |
| **All or 0 (ICPC)** | Subtask chỉ đạt điểm khi pass mọi test | Các test đầu trong batch có điểm `0`, test cuối có điểm `1`; batch dùng kiểu cộng điểm. |
| **Min (IOI)** | Điểm subtask bị giới hạn bởi test yếu nhất | Mỗi test có trọng số `1`; batch dùng kiểu lấy min. |

Sau khi tự điền, bạn vẫn có thể sửa điểm, pretest và cách tính điểm batch thủ công.

## 3. Checker {#checker}

Checker quyết định output của bài nộp có khớp với đáp án mong đợi hay không. Hãy ưu tiên checker có sẵn khi nó phù hợp với bài: cấu hình đơn giản hơn, không cần bảo trì code checker riêng. Dùng custom checker khi bài có nhiều đáp án đúng, cách tính điểm đặc biệt, hoặc format mà các checker có sẵn không biểu diễn được.

### 3.1. Default Checker {#default-checker}

Checker có sẵn không cần upload file checker. Chọn checker trong menu **Checker**, rồi điền checker arguments chỉ khi checker đó có tuỳ chọn bổ sung.

| Checker | Dùng khi | Ghi chú |
|---|---|---|
| `standard` | Hầu hết bài exact-output | So sánh theo token và bỏ qua khoảng trắng giữa các token. |
| `floats` | Output số thực có sai số | So sánh token số với sai số tuyệt đối hoặc tương đối. Đặt `precision` theo số chữ số thập phân yêu cầu. Token không phải số vẫn phải khớp chính xác. |
| `floatsabs` | Output số thực chỉ dùng sai số tuyệt đối | Giống `floats`, nhưng chỉ chấp nhận sai số tuyệt đối. |
| `floatsrel` | Output số thực chỉ dùng sai số tương đối | Giống `floats`, nhưng chỉ chấp nhận sai số tương đối. |
| `rstripped` | Khoảng trắng cuối dòng không quan trọng, nhưng cấu trúc dòng vẫn quan trọng | So sánh từng dòng sau khi bỏ khoảng trắng cuối dòng. Các khoảng trắng khác vẫn có ý nghĩa. |
| `sorted` | Các dòng output có thể xuất hiện theo thứ tự bất kỳ | Bỏ qua dòng rỗng, tách mỗi dòng không rỗng thành token, sắp xếp các dòng rồi so sánh. |
| `identical` | Output phải khớp từng byte | Dùng cho format rất chặt hoặc dữ liệu text/binary mà khoảng trắng phải chính xác. |
| `linecount` | Token phải khớp trên đúng từng dòng | Bỏ qua khoảng trắng thừa trong cùng một dòng, nhưng ranh giới dòng phải khớp. |
| `csv_accuracy`, `csv_rmse`, `csv_mae`, `csv_f1`, `csv_auc`, `csv_logloss` | Bài nộp CSV kiểu Kaggle | Xem [Bài kiểu Kaggle (CSV)](#kaggle-style-csv-problems) bên dưới để biết `checker_args` và cách tính điểm. |

Dùng **Testlib** hoặc **Testlib (CMS / IOI)** khi bạn đã có `checker.cpp` từ Polygon, IOI, CMS, hoặc package tương tự. Upload file đó ở trường checker C++. Với package IOI, xem [Import bài IOI](#importing-ioi-tasks).

### 3.2. Custom Checker {#custom-checker}

Định nghĩa cách chấm cho các bài có nhiều đáp án đúng hoặc format đặc biệt.

#### Python {#custom-checker-python}

Với custom checker Python, cài đặt hàm `check`:

```py
def check(process_output, judge_output, **kwargs):
    # return True/False
```

`**kwargs` có thể chứa: `process_output` (output bài nộp), `judge_output` (đáp án), `submission_source`, `judge_input`, `point_value`, `case_position`, `submission_language`, `execution_time`.

Trả về bool, hoặc `CheckerResult(passed, points, feedback='')` cho điểm thành phần.

**Ví dụ.** Input là một số nguyên `n`; output là hai số nguyên `a, b` bất kỳ thoả `a + b = n`.

```py
from dmoj.result import CheckerResult

def wa(feedback):
    return CheckerResult(False, 0, feedback)

def check(process_output, judge_output, judge_input, **kwargs):
    # process the input
    input_arr = judge_input.split()
    assert(len(input_arr) == 1)
    n = int(input_arr[0])

    #  process the contestant's output
    output_arr = process_output.split()

    if (len(output_arr) != 2):
        return wa('Wrong output format')

    try:
        a, b = int(output_arr[0]), int(output_arr[1])
    except:
        return wa('Wrong output format')

    if (n == a + b):
        return True
    return wa('a + b != n')
```

#### C++ {#custom-checker-cpp}

Viết một chương trình C++ chạy theo dạng `./main <input_file> <output_file> <ans_file>`.

**Mã thoát**: `0` = AC, `1` = WA, `2` = điểm thành phần (in tỷ lệ trong `[0,1]` ra **stderr**). Mọi thứ in ra **stdout** được hiển thị cho người nộp bài làm feedback.

**Ví dụ.** Cho `n`, chấp nhận mọi `a, b` thoả `a + b = n`. 100% điểm nếu cả hai không âm, 50% nếu có số âm.

```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char** argv) {
    ifstream inp(argv[1]);
    ifstream out(argv[2]);
    ifstream ans(argv[3]);

    int n, a, b, c, d;

    inp >> n;
    out >> a >> b;
    ans >> c >> d;

    if (a + b == c + d) {
        cout << a << " + " << b << " = " << c << " + " << d << endl;

        if (a >= 0 && b >= 0) {
            return 0; // AC
        }
        else {
            cerr << 0.5;
            return 2; // PARTIAL
        }
    }
    else {
        cout << "a + b = " << a + b << " != " << n << endl;
        return 1; // WA
    }
}
```

## 4. Interactive (C++) {#interactive}

Chương trình C++ chạy theo dạng `./main <input_file> <answer_file>`. Bài làm và interactor giao tiếp qua stdin/stdout.

**Mã thoát**: `0` = AC, `1` = WA, `2` = điểm thành phần (tỷ lệ ra **stderr**). Mọi thứ ghi ra **stderr** được hiển thị làm feedback.

**Ví dụ.** Đoán số: thí sinh phải tìm số bí mật `n` trong ≤ 31 câu hỏi. Mỗi truy vấn `x` nhận về `"SMALLER"`, `"BIGGER"` hoặc `"HOLA"`.

```cpp
#include <bits/stdc++.h>
using namespace std;

void quit(string reason) {
    cerr << reason << endl;
    exit(1);
}

void read(long long& guess) {
    if (!(cin >> guess)) exit(1); // Không có dòng này, chương trình sẽ đợi vô hạn
    if (guess < 1 || guess > 2e9) exit(1);
}

int main(int argc, char *argv[]) {
    ifstream inp(argv[1]);
    int N, guesses = 0;
    long long guess;
    inp >> N;

    while (guess != N && guesses <= 31) {
        read(guess);
        if (guess == N) {
            cout << "HOLA" << endl;
        } else if (guess > N) {
            cout << "SMALLER" << endl;
        } else {
            cout << "BIGGER" << endl;
        }
        guesses++;
    }

    cerr << "Number of used guesses: " << guesses << endl;

    if (guesses <= 31)
        return 0; // AC
    else {
        cerr << "Used too many guesses" << endl;
        return 1; // WA
    }
}
```

## 5. IOI Signature {#ioi-signature}

Thí sinh cài đặt một hàm; judge liên kết với handler do bạn cung cấp. Bạn chuẩn bị:
- **Header** (`.h`) — khai báo hàm (chỉ C/C++)
- **Handler** — chương trình đọc input, gọi hàm, in output

**Ví dụ.** Input là `t` rồi đến `t` số nguyên `n`. Thí sinh cài đặt `solve(int n)` trả về `n * 2`.

### C/C++ {#ioi-signature-cpp}

**Header (header.h):**
```cpp
#ifndef _HEADER_INCLUDED
#define _HEADER_INCLUDED
long long solve(long long n);
#endif
```

**Handler (handler.cpp):**
```cpp
#include <bits/stdc++.h>
#include "header.h"
using namespace std;


int main() {
    int t;
    cin >> t;
    for (int z = 1; z <= t; z++) {
        long long n;
        cin >> n;
        cout << solve(n) << "\\n";
    }

    return 0;
}
```

**Bài nộp của thí sinh:**
```cpp
int solve(int n) {
    return  n * 2;
}
```

### Python {#ioi-signature-python}
Bài nộp của thí sinh sẽ được lưu vào file _submission.py.

**Handler (handler.py):**
```python
from _submission import solve

def main():
    t = int(input())
    for _ in range(t):
        n = int(input())
        print(solve(n))

if __name__ == "__main__":
    main()
```

**Bài nộp của thí sinh:**
```python
def solve(n):
    return n * 2
```

### Java {#ioi-signature-java}
Học sinh phải đặt tên class đúng như yêu cầu của bài toán để handler sử dụng.

**Handler (handler.java):**
```java
import java.util.Scanner;

public class Handler {
    public static void main(String[] args) {
        Scanner scanner = new Scanner(System.in);
        int t = scanner.nextInt();
        for (int i = 0; i < t; i++) {
            int n = scanner.nextInt();
            System.out.println(Solution.solve(n));
        }
    }
}
```

**Bài nộp của thí sinh:**
```java
public class Solution {
    public static int solve(int n) {
        return n * 2;
    }
}
```

### Import bài IOI {#importing-ioi-tasks}

LQDOJ hỗ trợ đầy đủ các bài kiểu IOI: signature grader, chia subtask all-or-nothing, và bài tương tác / nhiều tiến trình.

1. **Test data** — upload ZIP test ở mục "File zip chứa test".
2. **Checker** — chọn **Checker** = **Testlib (CMS / IOI)** rồi upload `checker.cpp` của bài. **Trước khi upload, đổi `#include "testlib.h"` thành `#include "testlib_ioi.h"`** — IOI dùng một bản testlib tuỳ biến, trên judge đã được cài sẵn là `testlib_ioi.h`.
3. **Signature grader** — bật **Nộp bài bằng hàm?**, thêm một dòng cho mỗi ngôn ngữ kèm `grader.cpp` + file header của bài (ví dụ `festival.h`) — cùng giao diện như phần signature grader cơ bản ở trên.
4. **Bài tương tác** — nếu gói IOI có kèm `manager.cpp` (bài thuộc dạng tương tác), tick **Bài Communication**, upload `manager.cpp` sau khi cũng đổi `#include "testlib.h"` thành `#include "testlib_ioi.h"`, rồi đặt **Số tiến trình** = `1` cho bài tương tác thông thường hoặc `2` cho bài hai pha encode/decode.
5. **Chia subtask** — vào **Tự động điền test**, chọn chế độ **ICPC**, mỗi batch ứng với một subtask kèm tổng điểm. Chế độ ICPC chấm all-or-nothing cho mỗi batch — đúng kiểu IOI.

Bấm **Lưu** là bài có thể submit được.

**Bài mẫu trên site:**

- [IOI 2025 — Festival](https://ioinformatics.org/files/ioi2025problem4.pdf) — batch + signature grader + testlib checker (kiểu IOI chuẩn).
- [IOI 2025 — Souvenirs](https://ioinformatics.org/files/ioi2025problem1.pdf) — bài tương tác (một tiến trình bài làm giao tiếp với manager).
- [IOI 2025 — Migrations](https://ioinformatics.org/files/ioi2025problem5.pdf) — bài tương tác hai tiến trình (encode + decode).

## 6. Trình kiểm tra test {#testcase-validator}

Chương trình kiểm tra input của mỗi test có thoả ràng buộc của đề. Đọc stdin; exit `0` = hợp lệ, khác 0 = không hợp lệ (stderr được ghi lại làm feedback). Bấm **"Chạy kiểm tra"** để kiểm tra tất cả test.

### C++ {#testcase-validator-cpp}

```cpp
#include <bits/stdc++.h>
using namespace std;

int main() {
    int n;

    // Kiểm tra đọc được đúng một số nguyên
    if (!(cin >> n)) {
        cerr << "Không đọc được số nguyên n" << endl;
        return 1;
    }

    // Kiểm tra ràng buộc: 1 <= n <= 1000000
    if (n < 1 || n > 1000000) {
        cerr << "n = " << n << " nằm ngoài đoạn [1, 1000000]" << endl;
        return 1;
    }

    // Kiểm tra không có dữ liệu thừa
    string extra;
    if (cin >> extra) {
        cerr << "Dữ liệu thừa: " << extra << endl;
        return 1;
    }

    return 0; // Hợp lệ
}
```

### Python {#testcase-validator-python}

```python
import sys

def main():
    data = sys.stdin.read().split()

    # Kiểm tra có đúng một giá trị
    if len(data) != 1:
        print(f"Mong đợi 1 giá trị, nhận được {len(data)}", file=sys.stderr)
        sys.exit(1)

    # Kiểm tra đó là số nguyên
    try:
        n = int(data[0])
    except ValueError:
        print(f"'{data[0]}' không phải số nguyên", file=sys.stderr)
        sys.exit(1)

    # Kiểm tra ràng buộc: 1 <= n <= 1000000
    if not (1 <= n <= 1000000):
        print(f"n = {n} nằm ngoài đoạn [1, 1000000]", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)  # Hợp lệ

main()
```

## 7. Bài Output-only {#output-only}

Bài *output-only* không yêu cầu người giải viết chương trình thực thi — thay vào đó họ tải file input về, tính đáp án ở máy mình (bằng bất kỳ công cụ nào), rồi chỉ nộp file kết quả. Để cấu hình, hãy tick **Output-only?** trong biểu mẫu Test Data. Trang nộp bài khi đó sẽ chấp nhận một file `.zip` (file đơn được tự động đóng gói thành zip ở phía trình duyệt) và bộ chấm được chọn sẽ áp dụng lên nội dung bên trong.

> **Ngôn ngữ cho phép.** Hãy giới hạn **Ngôn ngữ cho phép** chỉ còn `Output` ở tab **Ngôn ngữ**. Nếu không, người giải sẽ thấy các ngôn ngữ khác trong menu nộp bài và có thể nộp mã nguồn, trong khi bộ chấm output-only không thể chấm mã nguồn.

> **Phân phối input test cho người giải.** Các file trong file zip Test Data là riêng tư cho hệ thống chấm — người giải không thấy được. Để cung cấp cho người giải các input họ cần để tính đáp án ở máy (ví dụ các test cho bài output-only kiểu IOI, hoặc file CSV training/test cho bài Kaggle), hãy tải lên qua tab **Tệp đính kèm** trên trang chỉnh sửa bài. Các tệp đính kèm sẽ hiển thị ở mục "Tệp" trên trang đề bài, với liên kết tải xuống tuân theo quyền truy cập thông thường của bài.

### 7.1. Output-only truyền thống (kiểu IOI) {#traditional-output-only}

Với mỗi test case, đặt tên file output kỳ vọng ở cột **Output file** (ví dụ `test01.out`). File zip người dùng nộp phải chứa một file có tên trùng khớp; bộ chấm được cấu hình (thường là `Standard`, `Floats`, hoặc một bộ chấm tùy chỉnh) sẽ so sánh output trong bài nộp với output kỳ vọng, giống như bài thường.

Định dạng này phù hợp khi đáp án là một file xác định cho mỗi test case (ví dụ độ dài đường đi ngắn nhất, một số nguyên, danh sách đã sắp xếp). Hãy chọn bộ chấm chuẩn hoặc tùy chỉnh phù hợp với loại output.

### 7.2. Bài kiểu Kaggle (CSV) {#kaggle-style-csv-problems}

Với các bài kiểu machine-learning nơi bài nộp là một file CSV chứa các dự đoán, được chấm so với đáp án ẩn bằng các chỉ số như độ chính xác hoặc RMSE, hãy chọn một trong các bộ chấm CSV có sẵn từ menu `Bộ chấm` — không cần viết code:

| Bộ chấm | Chỉ số | Hướng |
|---|---|---|
| `csv_accuracy` | độ chính xác (so khớp tuyệt đối trên cột nhãn) | càng cao càng tốt |
| `csv_rmse` | sai số bình phương trung bình (root mean squared error) | càng thấp càng tốt |
| `csv_mae` | sai số tuyệt đối trung bình | càng thấp càng tốt |
| `csv_f1` | macro F1 trên cột nhãn | càng cao càng tốt |
| `csv_auc` | ROC AUC nhị phân trên cột xác suất | càng cao càng tốt |
| `csv_logloss` | log loss trên cột xác suất | càng thấp càng tốt |

Bộ chấm đọc cả file đáp án và file người dùng nộp dưới dạng CSV, ghép theo cột `id_column`, rồi tính chỉ số trên cột `label_column`. Giá trị thô của chỉ số được hiển thị trong phản hồi của bài nộp.

**Chuẩn hoá điểm cho các chỉ số "càng thấp càng tốt"** (`csv_rmse`, `csv_mae`, `csv_logloss`):

- Khi đặt **`baseline`** trong `checker_args`: `điểm = max(0, 1 - giá_trị / baseline)`. Bài hoàn hảo (`giá_trị = 0`) đạt điểm 1.0; bài có giá trị bằng baseline đạt 0; tệ hơn nữa thì cũng kẹp ở 0. Dùng để hiệu chuẩn điểm theo ví dụ RMSE của một dự đoán tầm thường.
- Không đặt `baseline`: dùng công thức dự phòng `điểm = 1 / (1 + giá_trị)`. Đơn giản, không cần hiệu chuẩn, nhưng tỉ lệ điểm phụ thuộc vào miền giá trị tự nhiên của chỉ số.

#### `checker_args`

Khi chọn một bộ chấm `csv_*`, biểu mẫu sẽ hiện ra:

- **`id_column`** *(tuỳ chọn)* — tên cột định danh (hoặc chỉ số 0-based khi `has_header` không được tick). **Nếu để trống**, các hàng sẽ được khớp theo vị trí dòng — hữu ích khi CSV chỉ có một cột nhãn (ví dụ `y` mỗi dòng).
- **`label_column`** *(tuỳ chọn)* — tên (hoặc chỉ số) của cột nhãn / mục tiêu / xác suất. Mặc định là cột đầu tiên.
- **`has_header`** — tick nếu file CSV có hàng tiêu đề (mặc định: có).
- **`baseline`** *(tuỳ chọn, chỉ áp dụng cho chỉ số càng thấp càng tốt)* — một số dương xác định "giá trị tệ nhất tương ứng với 0 điểm". Ví dụ: với `csv_rmse`, đặt `baseline: 0.5` nghĩa là bài có RMSE ≥ 0.5 sẽ được 0 điểm, RMSE = 0 đạt điểm tối đa, ở giữa thì tuyến tính.

> **Mẹo — bài chỉ có một cột.** Nếu file đáp án và bài nộp chỉ chứa `y` (một giá trị trên mỗi dòng, không có cột `id`), hãy để trống cả `id_column` và `label_column`, và bỏ tick `has_header`. Bộ chấm sẽ so sánh từng dòng theo thứ tự.

#### Bảng xếp hạng public/private qua `pretest_fraction`

Để tổ chức kỳ thi kiểu Kaggle với bảng xếp hạng public hiển thị trong lúc thi và bảng private hé lộ khi kết thúc:

1. Đặt **`pretest_fraction`** trong `checker_args` thành một giá trị thuộc `(0, 1]` — ví dụ `0.5` để chấm trên 50% số dòng trong lúc thi.
2. Đánh dấu test case là **`is_pretest`** trong trình chỉnh sửa dữ liệu.
3. Ở phần kỳ thi, đặt **`run_pretests_only=True`** trên kỳ thi và đánh dấu bài là **`is_pretested`**.

Trong lúc kỳ thi chạy ở chế độ pretests-only, bộ chấm sẽ áp dụng `pretest_fraction` và chỉ chấm điểm trên một tập con các dòng được chọn theo hàm băm — người giải chỉ thấy điểm trên tập đó (bảng xếp hạng public). Việc chọn dòng được xác định bằng `md5(id)`, do đó cùng một tập con được dùng cho mọi bài nộp.

Sau khi kỳ thi kết thúc, hãy chuyển `run_pretests_only=False` trên kỳ thi rồi nhấn **Chấm lại tất cả bài nộp**. Bộ chấm khi đó sẽ bỏ qua `pretest_fraction` và chấm trên toàn bộ dòng — đó là bảng xếp hạng private.
