[TOC]

## 1. Test Generator

Generate tests with a C++ program instead of uploading them. The generator takes constraint args plus a seed; print the input to **stdout** and the expected output to **stderr**.

```bash
./generator [arg_1] [arg_2] ... [seed]
```

**Example.** Input: two integers `a, b` with `1 <= a, b <= 100000`. Output: `a + b`.

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

Appears under the generator file once saved. One line per test case — arguments are forwarded to the generator. Use **distinct seeds** to avoid duplicate tests.

For the `a + b` problem, a strong 10-test suite might cover small/medium/large ranges:

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

Click **"Fill testcases"** to materialize one test row per script line. Args show in the **Generator Args** column and can be edited inline; "Add new case" lets you add individual tests.

**Each test uses one source only** — either a file from the data ZIP or the generator. Don't forget to click **"Apply!"**.

## 2. Custom Checker

Define custom judging logic for problems with multiple valid answers or special output formats.

### Python

The default checker. Implement `check`:

```py
def check(process_output, judge_output, **kwargs):
    # return True/False
```

Available via `**kwargs`: `process_output` (submitter's output), `judge_output` (expected), `submission_source`, `judge_input`, `point_value`, `case_position`, `submission_language`, `execution_time`.

Return a bool, or `CheckerResult(passed, points, feedback='')` for partial credit.

**Example.** Input is one integer `n`; output any two integers `a, b` with `a + b = n`.

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

Compile a C++ program invoked as `./main <input_file> <output_file> <ans_file>`.

**Exit codes**: `0` = AC, `1` = WA, `2` = partial (print a ratio in `[0,1]` to **stderr**). Anything written to **stdout** is shown to the submitter as feedback.

**Example.** Given `n`, accept any `a, b` with `a + b = n`. Award 100% if both non-negative, 50% otherwise.

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

C++ program invoked as `./main <input_file> <answer_file>`. The submitter's binary and your interactor are connected via stdin/stdout pipes.

**Exit codes**: `0` = AC, `1` = WA, `2` = partial (ratio on **stderr**). Anything to **stderr** is shown as feedback.

**Example.** Guess-the-number: the contestant must find a secret `n` in ≤ 31 queries. Each query `x` gets `"SMALLER"`, `"BIGGER"`, or `"HOLA"`.

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

Contestants implement a function; the judge links it with your handler. You provide:
- **Header** (`.h`) — function declaration (C/C++ only)
- **Handler** — driver that reads input, calls the function, prints output

**Example.** Input is `t` followed by `t` integers `n`. Contestants implement `solve(int n)` returning `n * 2`.

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

### Importing IOI tasks

LQDOJ supports IOI-style tasks end-to-end: signature graders, subtask batching with all-or-nothing scoring, and interactive / multi-process tasks.

1. **Test data** — upload the test ZIP under "Data zip file".
2. **Checker** — set **Checker** to **Testlib (CMS / IOI)** and upload the task's `checker.cpp`. **Before uploading, change `#include "testlib.h"` to `#include "testlib_ioi.h"`** — IOI uses a customized testlib fork shipped on the judge as `testlib_ioi.h`.
3. **Signature grader** — tick **Is IOI signature**, then add one row per language with `grader.cpp` + the task header (e.g. `festival.h`) — same UI as the basic signature graders above.
4. **Interactive tasks** — if the IOI package ships a `manager.cpp` (the task is interactive), tick **Is communication**, upload `manager.cpp` after the same `testlib.h` → `testlib_ioi.h` edit, and set **Num processes** to `1` for a normal interactive task or `2` for a two-phase encode/decode task.
5. **Subtask batching** — in **Autofill testcases**, pick mode **ICPC**, one batch per subtask with the batch's total points. ICPC mode gives all-or-nothing scoring per batch — the standard IOI shape.

Hit **Apply!** and the problem is live.

**Sample problems on this site:**

- [IOI 2025 — Festival](https://ioinformatics.org/files/ioi2025problem4.pdf) — batch + signature grader + testlib checker (the standard IOI shape).
- [IOI 2025 — Souvenirs](https://ioinformatics.org/files/ioi2025problem1.pdf) — interactive task (one user process talking to a manager).
- [IOI 2025 — Migrations](https://ioinformatics.org/files/ioi2025problem5.pdf) — two-process interactive task (encode + decode phases).

## 5. Testcase Validator

A program that confirms each test input matches the problem's constraints. Reads stdin; exit `0` = valid, non-zero = invalid (stderr captured as feedback). Click **"Run Validator"** to check every test.

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

