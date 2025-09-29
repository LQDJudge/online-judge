[TOC]

## 1. Test Generator

Test Generator cho phép bạn sinh test bằng một chương trình generator viết bằng C++, thay vì upload file test như thông thường.

### Generator File (File sinh test)

Đầu tiên, bạn cần viết một generator file bằng C++ (có thể upload hoặc Edit trực tiếp) nhận argument là các số đại diện cho giới hạn của input và thêm một argument là seed dùng cho random. Chương trình sẽ random input từ giới hạn và seed được nhập từ argument.

Sau khi có input, bạn cần code lời giải bài toán trong file với input đó để tạo ra output. Cuối cùng, in input ra stdout (dùng cout) và in output ra stderr (dùng cerr).

Để test chương trình trên máy tính của bạn, bạn có thể sử dụng lệnh sau (Windows):

```bash
generator.exe [arg_1] [arg_2] ... [arg_n] [seed]
```

hoặc thay bằng `./generator` trên Linux/MacOS.

**Ví dụ:**
Dưới đây là ví dụ cho một bài toán: Input chứa 2 số nguyên a, b (1 <= a, b <= 100000). In ra tổng a + b.

```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int args_length, char* args[]) {
    if (args_length != 4) {
        cerr << "Usage: ./generator <x> <y> <global_seed>" << endl;
        return 1;
    }

    int x = stoi(args[1]); // cận dưới cho giới hạn của a và b
    int y = stoi(args[2]); // cận trên cho giới hạn của a và b
    int global_seed = stoi(args[3]); // seed để random

    if (x > y) {
        cerr << "Error: x should be less than or equal to y" << endl;
        return 1;
    }

    // Kết hợp global seed với x và y để tạo seed duy nhất
    int combined_seed = global_seed ^ (x * 31 + y * 37);

    // Khởi tạo random với seed đã tính
    mt19937 gen(combined_seed);
    uniform_int_distribution<> dist(x, y);

    // Input: Sinh hai số random a và b cho input
    int a = dist(gen);
    int b = dist(gen);

    // Output: Lời giải để tạo ra output
    int c = a + b;

    // In input ra stdout
    cout << a << " " << b << endl;

    // In output ra stderr
    cerr << c << endl;

    return 0;
}
```

### Generator Script (Script sinh test)

Generator Script sẽ hiện ra dưới mục Generator file sau khi bạn lưu file generator. Generator Script giúp bạn tạo ra bộ test nhanh chóng bằng cách nhập các argument (tham số), mỗi dòng tương ứng một test. Mỗi dòng argument được dùng trong generator file để sinh ra một test với input/output in ra từ file, từ đó tạo ra bộ test hoàn chỉnh.

Lấy ví dụ bài toán a + b. Bạn muốn tạo ra 10 test với giới hạn đa dạng để bao quát hết mọi trường hợp, một ví dụ về bộ test mạnh sẽ có 3 test với a, b nằm trong đoạn 1 đến 10, 3 test từ 100 đến 1000, và 4 test từ 10000 đến 100000. Với cách chia này, bộ test của bạn sẽ bao quát cho giá trị nhỏ lẫn giá trị lớn. 

**Lưu ý**, bạn nên để seed phân biệt cho các test để đảm bảo các test tạo ra sẽ không trùng nhau.

**Generator Script:**

```
1 10 12
1 10 5123
1 10 254
100 1000 51234
100 1000 4135
100 1000 123
10000 100000 456
10000 100000 4129
10000 100000 5912
10000 100000 4753
```

Sau khi có generator script, bạn hãy click nút "Fill testcases" (Điền tests) trong mục "Autofill testcases". Bảng testcases sẽ được thêm số lượng test bằng với số dòng trong script, mỗi test sẽ có argument (hiện trong Generator Args) từ dòng tương ứng trong script.

**Lưu ý**: Khi click "Fill testcases", nếu có file trong mục "Data zip file", testcases trong file cũng sẽ được thêm.

### Generator Args (Tham số sinh test)

Argument của mỗi test sẽ hiện ở mục generator args và bạn có thể thay đổi argument của test ở đó. 

Bạn cũng có thể thêm từng testcase bằng cách "Add new case" (Thêm test mới) và nhập argument vào mục generator args của test đó.

**Lưu ý quan trọng**
Mỗi test case chỉ sử dụng **một trong hai cách**:
- Hoặc lấy data từ file ZIP
- Hoặc sinh data từ generator + argument

Nhớ click **"Apply!"** ở cuối trang để lưu lại các thay đổi.

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

hoặc thay bằng ``` trên Linux/MacOS.

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

hoặc thay bằng ``` trên Linux/MacOS.

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