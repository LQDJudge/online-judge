[TOC]


## 1. Custom Checker (Python)

This is the default checker for the website, allowing users to update the most information (see details below). We need to complete the `check` function below:

```py
def check(process_output, judge_output, **kwargs):
    # return True/False
```

Where `**kwargs` can contain the following variables:

- `process_output`: output
- `judge_output`: expected answer
- `submission_source`: submission code
- `judge_input`: input
- `point_value`: points for the current test
- `case_position`: test case order
- `submission_language`: submission language
- `execution_time`: execution time

**Return:**

- Method 1: Return True/False
- Method 2: Return a `CheckerResult` object that can be called as `CheckerResult(case_passed_bool, points_awarded, feedback='')`

**Example:**
Below is an example for a problem: Input contains 1 integer n. Print two integers a, b such that a + b = n.

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

## 2. Custom Checker (C++)

To use this feature, you need to write a C++ program that takes 3 arguments in order: `input_file`, `output_file`, `ans_file` corresponding to input, output, and answer files.

To test the program on your computer, you can use the following command (Windows):

```bash
main.exe [input_file] [output_file] [ans_file]
```

or replace with `./main` on Linux/MacOS.

**Return:**
The program returns:

- 0 if AC (100% points)
- 1 if WA (0 points)
- 2 if partial points. In this case, print a real number in [0, 1] to stderr representing the point ratio. If points < 1, display WA; if points = 1, display AC.

Information written to stdout (using cout) will be displayed to the submitter (feedback).

**Example:**
The following program is used to judge a problem: Given n as a positive integer. Print two natural numbers a, b such that a + b = n.

If a + b = n and a, b >= 0, get 100% points; if a + b = n but one of a, b is negative, get 50% points.

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

To use this feature, you need to write a C++ program that takes 2 arguments: `input_file` `answer_file` corresponding to input and answer files (if needed).

To test the program on your computer as a contestant, you can use the following command (Windows):

```bash
main.exe [input_file] [answer_file]
```

or replace with `./main` on Linux/MacOS.

**Return:**
The program returns:

- 0 if AC (100% points)
- 1 if WA (0 points)
- 2 if partial points. In this case, print a real number in [0, 1] to stderr representing the point ratio. If points < 1, display WA; if points = 1, display AC.

Information printed to stderr (using cerr) will be feedback displayed to the user.

**Example:**
The following program is used to judge a guessgame problem: The player must find a secret number n (n is contained in the input file). Each time they ask a number x, and the program will return "SMALLER", "BIGGER" or "HOLA" based on the values of n and x. Need to find n after no more than 31 questions.

```cpp
#include <bits/stdc++.h>
using namespace std;

void quit(string reason) {
    cerr << reason << endl;
    exit(1);
}

void read(long long& guess) {
    if (!(cin >> guess)) exit(1); // Without this line, the program will wait indefinitely
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

This feature is used to implement functions like in IOI. Contestants are given a function definition and need to implement that function to return the correct value.

To use this feature, you need to write 2 programs:
- Header: This is the function definition file (extension must be `.h`, only applies to C/C++)
- Handler: This is the program that processes input and outputs based on the function

**Example:**
For a problem: input number n. Write function `solve(int n)` that returns `n * 2`. Assume input is multitest with format:
- First line contains `t` as number of tests
- Each line contains an integer `n`

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

**Student submission:**
```cpp
int solve(int n) {
    return  n * 2;
}
```

### b. Python
Student submission will be saved to file _submission.py.

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

### c. Java
Students must name the class correctly as required by the problem for the handler to use.

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