[TOC]

## 1. Test Generator / Sinh Test Tự Động

Tính năng Generator Test cho phép bạn tự động sinh dữ liệu test cho bài toán bằng cách sử dụng một chương trình generator viết bằng C++. Thay vì upload file test, bạn có thể viết code generator và cung cấp các tham số để tự động tạo ra nhiều test case khác nhau.

### Cách Thức Hoạt Động

Hệ thống generator hoạt động theo quy trình sau:

1. **Upload Generator Code**: Tạo file C++ để sinh test data
2. **Viết Generator Script**: Cung cấp tham số cho từng test case
3. **Tự động tạo test**: Hệ thống sử dụng generator + tham số để sinh test data khi có người dùng nộp bài

### Bước 1: Thêm Generator Code

**Khi chưa có Generator:**
- Truy cập trang quản lý test data của bài toán (ví dụ: `/problem/aplusb/test_data`)
- Bạn sẽ thấy hai tùy chọn:
  - **Upload file**: Tải lên file generator (.cpp) từ máy tính
  - **Edit**: Mở modal để viết code generator trực tiếp trên trang

**Sau khi thêm Generator:**
Khi đã có generator file, giao diện sẽ hiển thị thêm mục "Script sinh test" hay "Generator Script" để viết script sinh test.

### Bước 2: Viết Generator Script

Generator Script là nơi bạn cung cấp tham số cho từng test case. Mỗi dòng trong script đại diện cho một test case.

**Cách viết Generator Script:**
1. Click vào **"Chỉnh sửa script sinh test"** hay **"Edit Generator Script"**
2. Modal sẽ hiện ra với textarea có đánh số dòng
3. Mỗi dòng viết tham số cho một test case
4. Click **"Save"** để lưu

### Bước 3: Tạo Test Cases

**Có hai cách để thêm test cases:**

**Cách 1: Thêm từng test**
- Thêm từng test case bằng cách click **"Thêm test mới"** hay **"Add new case"**
- Nhập tham số vào mục **"Tham số sinh test"** hay **"Generator Args"** cho từng test

**Cách 2: Sử dụng Generator Script**
1. Viết Generator Script như hướng dẫn ở Bước 2
2. Click "Điền test" ở mục "Tự động điền test"
3. Hệ thống sẽ thêm số lượng test cases bằng với số dòng trong script
4. Mỗi test case sẽ có tham số từ dòng tương ứng trong script

⚠️ **Lưu ý**: Khi click "Điền test", nếu có file trong mục "File zip chứa test", testcases trong file cũng sẽ được thêm. Bạn cũng có thể chỉnh sửa tham số của từng test trong "Tham số sinh test" sau khi "Điền test"

⚠️ **Lưu ý**: Nhớ click **"Lưu"** hoặc **"Save"** cuối trang để lưu toàn bộ dữ liệu bài toán!

### Ví Dụ Hoàn Chỉnh: Bài Toán A + B (aplusb)

**Generator Code (C++):**
```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int args_length, char* args[]) {
    if (args_length != 4) {
        cerr << "Usage: ./generator <x> <y> <global_seed>" << endl;
        return 1;
    }

    int x = stoi(args[1]);
    int y = stoi(args[2]);
    int global_seed = stoi(args[3]); // Seed chung cho toàn bộ test

    if (x > y) {
        cerr << "Error: x should be less than or equal to y" << endl;
        return 1;
    }

    // Kết hợp global seed với x và y để tạo seed duy nhất
    int combined_seed = global_seed ^ (x * 31 + y * 37);

    // Khởi tạo random với seed đã tính
    mt19937 gen(combined_seed);
    uniform_int_distribution<> dist(x, y);

    // Sinh hai số ngẫu nhiên a và b
    int a = dist(gen);
    int b = dist(gen);

    // Output dữ liệu đầu vào (a và b)
    cout << a << " " << b << endl;

    // Output đáp án (a + b) ra stderr để debug
    cerr << (a + b) << endl;

    return 0;
}
```

**Generator Script:**
```
1 10 12
1 10 5123
1 10 254
100 200 51234
100 200 4135
100 200 123
1000 2000 456
1000 2000 4129
1000 2000 5912
1000 2000 4753
```

Với script này, khi click "Thêm test mới", hệ thống sẽ tạo 10 test cases:
- Test 1: generator chạy với tham số `1 10 12`
- Test 2: generator chạy với tham số `1 10 5123`
- ...

### Bảng bộ Test

Sau khi tạo test cases, bạn sẽ thấy bảng hiển thị các test với:
- **File từ ZIP**: Test data lấy từ file ZIP upload
- **Generator Args**: Tham số để chạy generator

Mỗi test case chỉ sử dụng **một trong hai cách**:
- Hoặc lấy data từ file ZIP
- Hoặc sinh data từ generator + tham số

### Lưu Ý Quan Trọng

1. **Generator code** phải nhận tham số từ command line arguments
2. **Output** của generator phải in ra `stdout` (dữ liệu input cho test)
3. **Expected output** có thể in ra `stderr` để debug
4. **Seed ngẫu nhiên** nên được thiết kế để đảm bảo tính deterministic
5. Nhớ click **"Lưu"** sau khi hoàn thành tất cả các bước

## 2. Custom Checker

Custom Checker cho phép bạn tự định nghĩa cách thức chấm bài toán thay vì chỉ so sánh output trực tiếp. Điều này rất hữu ích cho các bài toán có nhiều đáp án đúng hoặc cần kiểm tra format đặc biệt.

### Python

Đây là checker mặc định của website, cho phép người dùng cập nhật nhiều thông tin nhất (xem chi tiết bên dưới). Chúng ta cần hoàn thành hàm `check` bên dưới:

```py
def check(process_output, judge_output, **kwargs):
    # return True/False
```

Trong đó `**kwargs` có thể chứa các biến sau:

- `process_output`: output
- `judge_output`: đáp án
- `submission_source`: mã nguồn bài nộp
- `judge_input`: input
- `point_value`: điểm của test hiện tại
- `case_position`: thứ tự test case
- `submission_language`: ngôn ngữ bài nộp
- `execution_time`: thời gian thực thi

**Trả về:**

- Cách 1: Trả về True/False
- Cách 2: Trả về một đối tượng `CheckerResult` có thể gọi dưới dạng `CheckerResult(case_passed_bool, points_awarded, feedback='')`

**Ví dụ:**
Dưới đây là ví dụ cho một bài toán: Input chứa 1 số nguyên n. In ra hai số nguyên a, b sao cho a + b = n.

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

### C++

Để sử dụng tính năng này, bạn cần viết một chương trình C++ nhận 3 argument theo thứ tự: `input_file`, `output_file`, `ans_file` tương ứng với file input, output và đáp án.

Để test chương trình trên máy tính của bạn, bạn có thể sử dụng lệnh sau (Windows):

```bash
main.exe [input_file] [output_file] [ans_file]
```

hoặc thay bằng `./main` trên Linux/MacOS.

**Trả về:**
Chương trình trả về:

- 0 nếu AC (100% điểm)
- 1 nếu WA (0 điểm)
- 2 nếu điểm một phần. Trường hợp này, in ra một số thực trong [0, 1] ra stderr đại diện cho tỷ lệ điểm. Nếu điểm < 1, hiển thị WA; nếu điểm = 1, hiển thị AC.

Thông tin ghi ra stdout (bằng cout) sẽ được hiển thị cho người nộp bài (feedback).

**Ví dụ:**
Chương trình sau được dùng để chấm một bài toán: Cho n là một số nguyên dương. In ra hai số tự nhiên a, b sao cho a + b = n.

Nếu a + b = n và a, b >= 0, được 100% điểm; nếu a + b = n nhưng một trong a, b âm, được 50% điểm.

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

## 3. Interactive (C++)

Để sử dụng tính năng này, bạn cần viết một chương trình C++ nhận 2 argument: `input_file` `answer_file` tương ứng với file input và đáp án (nếu cần).

Để test chương trình trên máy tính của bạn với tư cách là thí sinh, bạn có thể sử dụng lệnh sau (Windows):

```bash
main.exe [input_file] [answer_file]
```

hoặc thay bằng `./main` trên Linux/MacOS.

**Trả về:**
Chương trình trả về:

- 0 nếu AC (100% điểm)
- 1 nếu WA (0 điểm)
- 2 nếu điểm một phần. Trường hợp này, in ra một số thực trong [0, 1] ra stderr đại diện cho tỷ lệ điểm. Nếu điểm < 1, hiển thị WA; nếu điểm = 1, hiển thị AC.

Thông tin in ra stderr (bằng cerr) sẽ là feedback hiển thị cho người dùng.

**Ví dụ:**
Chương trình sau được dùng để chấm một bài toán guessgame: Người chơi phải tìm ra một số bí mật n (n có trong file input). Mỗi lần họ hỏi một số x, và chương trình sẽ trả lời "SMALLER", "BIGGER" hoặc "HOLA" dựa trên giá trị của n và x. Cần tìm ra n sau không quá 31 câu hỏi.

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

## 4. IOI Signature

Tính năng này được sử dụng để implement function như trong IOI. Thí sinh được cho một định nghĩa function và cần implement function đó để trả về giá trị đúng.

Để sử dụng tính năng này, bạn cần viết 2 chương trình:
- Header: Đây là file định nghĩa function (extension phải là `.h`, chỉ áp dụng với C/C++)
- Handler: Đây là chương trình xử lý input và output dựa trên function

**Ví dụ:**
Cho một bài toán: input số n. Viết function `solve(int n)` trả về `n * 2`. Giả sử input là multitest với format:
- Dòng đầu chứa `t` là số test
- Mỗi dòng chứa một số nguyên `n`

### C/C++

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

**Student submission:**
```cpp
int solve(int n) {
    return  n * 2;
}
```

### Python
Student submission sẽ được lưu vào file _submission.py.

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

**Student submission:**
```python
def solve(n):
    return n * 2
```

### Java
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

**Student submission:**
```java
public class Solution {
    public static int solve(int n) {
        return n * 2;
    }
}
```