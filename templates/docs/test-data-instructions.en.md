[TOC]

## 1. Test Generator

Test Generator allows you to generate tests using a generator program written in C++, instead of uploading test files as usual.

### Generator File

First, you need to write a generator file in C++ (can be uploaded or edited directly) that takes as arguments representing the constraint of the input and adds an argument as the seed used for random. The program will randomize the input from the constraint and the seed is entered from the argument.

Once you have the input, you need to code the solution with that input in the file to create the output. Finally, print the input to stdout (use cout) and print the output to stderr (use cerr).

To test the program on your computer, you can use the following command (Windows):

```bash
generator.exe [arg_1] [arg_2] ... [arg_n] [seed]
```

or with `./generator` on Linux/MacOS.

**Ví dụ:**
Here is an example of a problem: Input contains 2 integers a, b (1 <= a, b <= 100000). Print the sum a + b.

```cpp
#include <bits/stdc++.h>
using namespace std;

int main(int args_length, char* args[]) {
    if (args_length != 4) {
        cerr << "Usage: ./generator <x> <y> <global_seed>" << endl;
        return 1;
    }

    int x = stoi(args[1]); // lower bound for the limits of a and b
    int y = stoi(args[2]); // upper bound for the limits of a and b
    int global_seed = stoi(args[3]); // random seed

    if (x > y) {
        cerr << "Error: x should be less than or equal to y" << endl;
        return 1;
    }

    // Combine global seed with x and y to create unique seed
    int combined_seed = global_seed ^ (x * 31 + y * 37);

    // Initialize random with computed seed
    mt19937 gen(combined_seed);
    uniform_int_distribution<> dist(x, y);

    // Input: Generate two random integers a and b
    int a = dist(gen);
    int b = dist(gen);

    // Output: Solution to create output
    int c = a + b;

    // Print input to stdout
    cout << a << " " << b << endl;

    // Print output to stderr
    cerr << c << endl;

    return 0;
}
```

### Generator Script

Generator Script will appear under Generator file after you save the generator file. Generator Script helps you create a test suite quickly by entering arguments, each line corresponding to a test. Each argument line is used in the generator file to generate a test with input/output printed from the file, thereby creating a complete test suite.

Take the problem a + b for example. You want to create 10 test cases with diverse constraints to cover all cases, an example of a strong test suite would have 3 tests with a, b in the range 1 to 10, 3 tests from 100 to 1000, and 4 tests from 10000 to 100000. With this division, your test suite will cover both small and large values.

**Note**, you should leave a distinct seed for the tests to ensure that the generated tests will not be duplicated.

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

After having the generator script, click the "Fill testcases" button in the "Autofill testcases" section. The testcases table will be added with the number of tests equal to the number of lines in the script, each test will have arguments (shown in Generator Args) from the corresponding line in the script. 

**Note**: When clicking "Fill testcases", if there is a file in the "Data zip file" section, the testcases in the file will also be added.

### Generator Args

The arguments of each test will be shown in the generator args section and you can change the arguments of the test there. 

You can also add individual testcases by "Add new case" and enter arguments in the generator args section of that test.

**Important Note**
Each test case only uses **one of two ways**:
- Either get data from ZIP file
- Or generate data from generator + argument

Remember to click **"Apply!"** at the bottom of the page to save the changes.

## 2. Custom Checker

Custom Checker allows you to define custom judging logic instead of just direct output comparison. This is very useful for problems with multiple correct answers or special format requirements.

### Python

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

### C++

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

### Java
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

## 5. Testcase Validator

Testcase Validator allows you to validate that your test input data satisfies the problem's constraints. The validator program reads the test input from stdin and returns exit code 0 if the input is valid, or a non-zero exit code if the input is invalid. This helps catch errors in test data before contestants encounter them.

To use this feature, upload or edit a validator program in the **Testcase validator** field.

### How It Works

1. The validator receives the test input on **stdin**
2. It checks whether the input satisfies the problem's constraints
3. **Exit code 0** = input is valid
4. **Non-zero exit code** = input is invalid
5. Any output to **stderr** is captured as feedback (explains why validation failed)

After saving the validator, click **"Run Validator"** to run the validator against all test cases. The results will show which test cases pass or fail validation.

### C++

```cpp
#include <bits/stdc++.h>
using namespace std;

int main() {
    int n;

    // Check that we can read exactly one integer
    if (!(cin >> n)) {
        cerr << "Cannot read integer n" << endl;
        return 1;
    }

    // Check constraints: 1 <= n <= 1000000
    if (n < 1 || n > 1000000) {
        cerr << "n = " << n << " is out of range [1, 1000000]" << endl;
        return 1;
    }

    // Check no extra data
    string extra;
    if (cin >> extra) {
        cerr << "Unexpected extra data: " << extra << endl;
        return 1;
    }

    return 0; // Valid
}
```

### Python

```python
import sys

def main():
    data = sys.stdin.read().split()

    # Check that we have exactly one token
    if len(data) != 1:
        print(f"Expected 1 value, got {len(data)}", file=sys.stderr)
        sys.exit(1)

    # Check that it's an integer
    try:
        n = int(data[0])
    except ValueError:
        print(f"'{data[0]}' is not an integer", file=sys.stderr)
        sys.exit(1)

    # Check constraints: 1 <= n <= 1000000
    if not (1 <= n <= 1000000):
        print(f"n = {n} is out of range [1, 1000000]", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)  # Valid

main()
```

## 6. Output-only Problems

Output-only problems don't require solvers to write a runnable program — instead they download the input data, compute the answer locally (with whatever tools they like), and submit just the result file. To configure one, tick **Is output only** in the test data form. The submit page then accepts a `.zip` (single files are auto-zipped client-side) and the chosen checker is applied to its contents.

> **Allowed language.** Make sure to restrict the **Allowed languages** to just `Output` on the **Languages** tab. Otherwise solvers will see other languages in the submit dropdown and submit source code, which the output-only checker can't grade.

> **Distributing the test inputs to solvers.** Files inside the test-data zip are private to the judge — solvers can't see them. To give solvers the inputs they need to compute answers locally (e.g. the test cases for an IOI-style output-only problem, or the training/test CSV for a Kaggle problem), upload them via the **Attachments** tab on the problem edit page. Attachments appear in a "Files" section on the problem statement page, with download links scoped to the problem's normal access permissions.

### 6.1. Traditional output-only (IOI-style)

For each test case, name the expected output file in the **Output file** column (e.g. `test01.out`). The submitter's zip must contain a file with the matching name; the configured checker (typically `Standard`, `Floats`, or a custom one) is then applied to compare submission output vs. expected output, the same as for a normal problem.

This format is appropriate when the answer is a single deterministic file per test case (e.g. shortest-path lengths, integer answers, sorted lists). Pick whichever standard or custom checker fits the output type.

### 6.2. Kaggle-style CSV problems

For machine-learning–style problems where the submission is a CSV of predictions to be scored against a hidden answer key with a metric like accuracy or RMSE, use one of the built-in CSV checkers from the `Checker` dropdown — no custom code needed:

| Checker | Metric | Direction |
|---|---|---|
| `csv_accuracy` | exact-match accuracy on the label column | higher is better |
| `csv_rmse` | root mean squared error on a numeric column | lower is better |
| `csv_mae` | mean absolute error on a numeric column | lower is better |
| `csv_f1` | macro F1 on the label column | higher is better |
| `csv_auc` | binary ROC AUC on a probability column | higher is better |
| `csv_logloss` | log loss on a probability column | lower is better |

The checker reads both the answer key and the submission as CSV, joins on `id_column`, and computes the metric on `label_column`. The raw metric value is shown in the submission feedback.

**Score normalization for lower-better metrics** (`csv_rmse`, `csv_mae`, `csv_logloss`):

- With **`baseline`** set in `checker_args`: `score = max(0, 1 - value / baseline)`. A perfect submission (`value = 0`) scores 1.0; a submission at the baseline (`value = baseline`) scores 0; anything worse is clamped to 0. Use this to calibrate scoring against e.g. the trivial-prediction RMSE.
- Without `baseline`: fallback `score = 1 / (1 + value)`. Simple, no calibration, but score scaling depends on the metric's natural range.

#### `checker_args`

When you select a `csv_*` checker the form exposes:

- **`id_column`** *(optional)* — name (or 0-based index when `has_header` is off) of the row identifier column. **If omitted**, rows are aligned by row index — useful when the CSV is just a single column of labels (e.g. `y` per line).
- **`label_column`** *(optional)* — name (or index) of the label / target / probability column. Defaults to the first column.
- **`has_header`** — checked if your CSVs have a header row (default: yes).
- **`baseline`** *(optional, lower-better metrics only)* — a positive number defining "the worst score worth zero points". E.g., for `csv_rmse` setting `baseline: 0.5` means a submission with RMSE ≥ 0.5 scores 0, RMSE = 0 scores full points, with linear scaling in between.

> **Tip — single-column predictions.** If the answer key and submissions are just `y` (one value per line, no `id`), leave both `id_column` and `label_column` blank and uncheck `has_header`. The checker will compare row-by-row.

#### Public / Private leaderboard via `pretest_fraction`

To run a Kaggle-style contest with a public leaderboard during the contest and a private leaderboard revealed at the end:

1. Set **`pretest_fraction`** in checker_args to a value in `(0, 1]` — e.g. `0.5` to score on 50% of rows during the contest.
2. Mark the test case as **`is_pretest`** in the test data editor.
3. On the contest, set **`run_pretests_only=True`** and mark the contest problem as **`is_pretested`**.

While the contest runs in pretests-only mode, the checker honors `pretest_fraction` and scores only a deterministic hash-selected subset of rows — solvers see scores only on that subset (the public leaderboard). Row selection is keyed off `md5(id)`, so the same subset is used for every submission.

After the contest ends, flip `run_pretests_only=False` on the contest and click **Rejudge all submissions**. The checker then ignores `pretest_fraction` and scores all rows — that's the private leaderboard.
