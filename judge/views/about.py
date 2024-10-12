from django.shortcuts import render
from django.utils.translation import gettext as _


def about(request):
    return render(
        request,
        "about/about.html",
        {
            "title": _("About"),
        },
    )


def custom_checker_sample(request):
    content = """
1. Trình chấm tự viết (PY)
2. Trình chấm tự viết (CPP)
3. Interactive (CPP)
4. Dùng hàm như IOI

---

##1. Trình chấm tự viết (PY)
Đây là checker mặc định của website, cho phép người dùng cập nhật được nhiều thông tin nhất (chi tiết xem ở bên dưới). Chúng ta cần hoàn thành hàm `check` dưới đây:
```py
def check(process_output, judge_output, **kwargs):
    # return True/False
```

Trong đó, `**kwargs` có thể chứa các biến sau:

- `process_output`: output
- `judge_output`: đáp án
- `submission_source`: Code bài nộp
- `judge_input`: input
- `point_value`: điểm của test đang chấm
- `case_position`: thứ tự của test
- `submission_language`: ngôn ngữ của bài nộp
- `execution_time`: thời gian chạy

**Return**:

- Cách 1: Trả về True/False
- Cách 2: Trả về một object `CheckerResult` có thể được gọi như sau `CheckerResult(case_passed_bool, points_awarded, feedback='')`

**Ví dụ:**
Dưới đây là ví dụ cho bài toán: Input gồm 1 số nguyên n. In ra 2 số nguyên a, b sao cho a + b = n.

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

## 2. Trình chấm tự viết (CPP)

Để sử dụng chức năng này, cần viết một chương trình C++ pass vào 3 arguments theo thứ tự `input_file`, `output_file`, `ans_file` tương ứng với các file input, output, đáp án.

Để test chương trình trên máy tính, có thể dùng lệnh như sau (Windows):

```bash
main.exe [input_file] [output_file] [ans_file]
```

hoặc thay bằng `./main` trên Linux/MacOS.

**Return:**
Chương trình trả về giá trị:

- 0 nếu AC (100% điểm)
- 1 nếu WA (0 điểm)
- 2 nếu điểm thành phần. Khi đó cần in ra stderr một số thực trong đoạn [0, 1] thể hiện cho tỷ lệ điểm. Nếu điểm < 1 thì hiển thị WA, điểm = 1 thì hiển thị AC.
Những thông tin được viết ra stdout (bằng cout) sẽ được in ra màn hình cho người nộp bài(feedback)

**Ví dụ:**
Chương trình sau dùng để chấm bài toán: Cho n là một số nguyên dương. In ra hai số tự nhiên a, b sao cho a + b = n.

Nếu in ra a + b = n và a, b >= 0 thì được 100% số điểm, nếu a + b = n nhưng một trong 2 số a, b âm thì được 50% số điểm.

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

## 3. Interactive (CPP)
Để sử dụng chức năng này, cần viết một chương trình C++ pass vào 2 arguments `input_file` `answer_file` tương ứng file input và đáp án (nếu cần thiết).

Để test chương trình trên máy tính với tư cách thí sinh, có thể dùng lệnh như sau (Windows):

```bash
main.exe [input_file] [answer_file]
```

hoặc thay bằng `./main` trên Linux/MacOS.

**Return:**
Chương trình trả về giá trị:

- 0 nếu AC (100% điểm)
- 1 nếu WA (0 điểm)
- 2 nếu điểm thành phần. Khi đó cần in ra stderr một số thực trong đoạn [0, 1] thể hiện cho tỷ lệ điểm. Nếu điểm < 1 thì hiển thị WA, điểm = 1 thì hiển thị AC.
Thông tin được in ra trong stderr (bằng cerr) sẽ là feedback hiển thị cho người dùng.

**Ví dụ:**
Chương trình sau dùng để chấm bài toán guessgame: Người chơi phải tìm 1 số bí mật n (n chứa trong file input). Mỗi lần họ được hỏi một số x, và chương trình sẽ trả về "SMALLER", "BIGGER" hoặc "HOLA" dựa trên giá trị của n và x. Cần tìm ra n sau không quá 31 câu hỏi.

```cpp
#include <bits/stdc++.h>
using namespace std;

void quit(string reason) {
    cerr << reason << endl;
    exit(1);
}

void read(long long& guess) {
    if (!(cin >> guess)) exit(1); // Nếu không có dòng này, chương trình sẽ chờ vô hạn
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
Đây là chức năng để sử dụng hàm như trong IOI. Thí sinh được cho một định nghĩa hàm và cần cài đặt hàm đó trả về giá trị đúng.
Để sử dụng chức năng này, cần viết 2 chương trình:
- Header: Đây là file định nghĩa hàm (đuôi phải là `.h`, chỉ áp dụng cho C/C++)
- Handler: Đây là chương trình xử lý input và xuất ra output dựa trên hàm

**Ví dụ:**
Cho bài toán: nhập vào số n. Viết hàm `solve(int n)` trả về `n * 2`. Giả sử input là multitest có dạng:
- Dòng đầu chứa `t` là số test
- Mỗi dòng chứa một số nguyên `n`

### a. C/C++

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

**Bài nộp thí sinh:**
```cpp
int solve(int n) {
    return  n * 2;
}
```

### b. Python
Bài nộp thí sinh sẽ được lưu vào file _submission.py.

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

**Bài nộp thí sinh:**
```python
def solve(n):
    return n * 2
```

### c. Java
Thí sinh phải đặt tên class đúng như yêu cầu đề bài để handler sử dụng

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

**Bài nộp thí sinh:**
```java
public class Solution {
    public static int solve(int n) {
        return n * 2;
    }
}
```


"""
    return render(
        request,
        "about/custom-checker-sample.html",
        {
            "title": _("Custom Checker Sample"),
            "content": content,
        },
    )
