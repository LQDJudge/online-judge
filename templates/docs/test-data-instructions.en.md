[TOC]

## 1. Start here {#start-here}

The Test Data page turns your test files, generators, checkers, and scoring rules into the `init.yml` used by the judge. Most problems only need three things:

1. Add the tests, either by uploading a ZIP in **Data zip file** or by uploading a C++ **Generator file**.
2. Click **Fill testcases** or use **Add new case** until the **Test Cases** table has one real row for each test.
3. Choose a **Checker**, review the points and pretest flags, then click **Apply!**.

Rows with type **Normal case** are real tests. **Batch start** and **Batch end** rows only group tests for subtask scoring; they are not run as tests themselves. Each normal case should use exactly one source: either **Input file** / **Output file** from the ZIP, or **Generator Args** for the generator.

Clicking **Apply!** saves the files and rows, generates `init.yml`, and notifies judges that the problem data changed. If the page shows feedback above the form, fix that issue and click **Apply!** again.

### UI map {#ui-map}

Use this table as the source of truth for what each control on the Test Data page does.

| UI control | What to put there | When to use it |
|---|---|---|
| **Data zip file** | A `.zip` containing private input/output files. | You already have `.in` / `.out` style files. |
| **Generator file** | A C++ file, usually named `gen.cpp`. | You want the judge to generate tests from arguments instead of storing all files. |
| **Generator Script** | One line of arguments per generated test. Open it from the generator row after saving a generator. | You have a generator and want many generated rows. |
| **Checker** | The judging method: `standard`, `floats`, `customcpp`, `testlib`, `csv_*`, etc. | Every problem needs a checker; `standard` is fine for most exact-output tasks. |
| **Checker arguments** | JSON options for the selected checker. The UI shows helper inputs for float and CSV checkers. | Needed for tolerances, CSV columns, public/private leaderboard splits, and other checker-specific options. |
| **Custom cpp checker file** | C++ checker or Testlib checker source. | Use with **Custom checker (CPP)**, **Testlib**, or **Testlib (CMS / IOI)**. |
| **Interactive judge** | C++ interactor source. | Use with **Interactive** or **Interactive (Testlib)**. |
| **Input file name** / **Output file name** | File names used by submissions instead of stdin/stdout. | Only for file-I/O problems. Leave blank for normal stdin/stdout. |
| **Is output only** | Checkbox. | Contestants submit output files instead of source code. |
| **Binary answer data** | Checkbox. | Expected answers or submitted output files are binary, such as `.npy`, `.npz`, images, or archives. |
| **Output submission size limit (MB)** | Maximum output-only ZIP size. | Output-only problems with larger result files. |
| **Is IOI signature** | Checkbox that reveals language-specific signature grader rows. | Contestants implement a function instead of a full program. |
| **Is communication** | Checkbox for IOI-style manager/user multi-process tasks. | Interactive IOI packages with `manager.cpp`. |
| **Manager** and **Num processes** | Manager source and number of user processes. | Communication tasks only. |
| **Testcase validator** | C++ or Python validator source. | You want to check every generated/uploaded input against constraints. |
| **Autofill testcases** | Batch mode, batch starts, **Fill testcases**, and **Or use custom JSON**. | Quickly create or replace rows in the **Test Cases** table. |
| **Test Cases** table | The final list of cases and batches used by the judge. | Always review this table before clicking **Apply!**. |

The **Test Cases** table has these columns:

| Column | Meaning |
|---|---|
| **Type** | `Normal case` runs one test. `Batch start` begins a subtask. `Batch end` closes it. |
| **Input file** | ZIP file used as judge input. Leave empty for generator-only rows. |
| **Output file** | ZIP file used as expected output. Leave empty for generator-only rows. |
| **Points** | For normal rows, the case weight. For batch-start rows, the total batch score. |
| **Pretest?** | Whether the case is visible in pretest-only judging. For batched tests, set this on the batch-start row. |
| **Generator Args** | Arguments passed to the generator for this test. Leave empty for ZIP-backed rows. |
| **Delete?** | Removes the row when you click **Apply!**. |

## 2. Adding Tests {#adding-tests}

### 2.1. Upload a ZIP {#zip-test-data}

Use **Data zip file** when you already have `.in` / `.out` files. The file names in the **Input file** and **Output file** columns must exist in the ZIP. After uploading the ZIP, click **Fill testcases** to pair input and output files automatically, or use **Add new case** and select the files manually.

Keep the ZIP as plain test data. The ZIP is private to the judge; if contestants need to download inputs for an output-only or Kaggle-style problem, upload those public files from the problem edit page's **Attachments** tab instead.

### 2.2. Use a Generator {#test-generator}

Use a generator when the test data is easier to describe by constraints than by storing every `.in` / `.out` file. The generator is a C++ program. It receives command-line arguments, usually constraint parameters plus a seed, then prints:

- the test input to **stdout**
- the expected answer to **stderr**

```bash
./generator [arg_1] [arg_2] ... [seed]
```

The generator time limit is the problem time limit, so write it like normal competitive-programming code. Use fast I/O for `cout` and disable automatic flushing for `cerr`:

```cpp
ios::sync_with_stdio(false);
cin.tie(nullptr);
cerr.unsetf(ios::unitbuf);
```

Also prefer `'\n'` over `endl`. This matters because expected output is written to `cerr`; by default `cerr` flushes after each operation, which can make large generated answers unnecessarily slow.

**Example.** Input: two integers `a, b` with `1 <= a, b <= 100000`. Output: `a + b`.

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

### 2.3. Generator Script {#generator-script}

After saving a generator file, use **Generator Script** to create many generated tests quickly. One non-empty, non-comment line becomes one normal test case. The line is split by spaces and passed directly to the generator. Use distinct seeds unless you intentionally want duplicate tests.

For the `a + b` problem, this 10-test suite covers small, medium, and large ranges:

```
# Small values
1 10 12
1 10 5123
1 10 254

# Medium values
100 1000 51234
100 1000 4135
100 1000 123

# Large values
10000 100000 456
10000 100000 4129
10000 100000 5912
10000 100000 4753
```

Click **Fill testcases** to materialize one test row per script line. The arguments appear in the **Generator Args** column and can be edited inline. **Add new case** can add one generated case manually.

### 2.4. Custom JSON autofill {#custom-json-autofill}

Use **Or use custom JSON** when the exact rows are already known or generated by another tool. Each row should use either `testcase` for ZIP-backed data or `generator_args` for generated data, not both.

Non-batched example:

```json
[
  {"score": 1, "testcase": "1"},
  {"score": 1, "generator_args": "2 --small"},
  {"score": 2, "testcase": "complete-small-01"}
]
```

Batched example:

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

JSON autofill only fills rows on the page. Review the result, then click **Apply!**.

### 2.5. Batches and scoring {#batches-and-scoring}

Use batches for subtasks. Enter **Batch start positions** as the first test number of each batch. For example, `1, 5, 9` creates batches `[1..4]`, `[5..8]`, and `[9..end]`.

The **Batch mode** dropdown controls how **Fill testcases** assigns points:

| Mode | Use when | What it creates |
|---|---|---|
| **Sum (VOI)** | Each case contributes independently | Every case gets weight `1`; the batch score is the sum of passed case weights. |
| **All or 0 (ICPC)** | A subtask should pass only if every case passes | Earlier cases in the batch get `0`, the last case gets `1`; the batch uses sum scoring. |
| **Min (IOI)** | A subtask score should be limited by the weakest case | Every case gets weight `1`; the batch uses min scoring. |

You can still edit points, pretest flags, and batch scoring manually after autofill.

## 3. Checker {#checker}

The checker decides whether a submission's output matches the expected answer. Prefer a built-in checker when it matches the problem: it is simpler, faster to configure, and does not require maintaining checker source code. Use a custom checker when the problem has multiple valid answers, special scoring, or a format that the built-in checkers cannot express.

### 3.1. Default Checker {#default-checker}

Built-in checkers do not need a checker file. Select the checker in the **Checker** dropdown, then fill checker arguments only when the selected checker exposes extra options.

| Checker | Use when | Notes |
|---|---|---|
| `standard` | Most exact-output problems | Compares tokens and ignores whitespace between tokens. |
| `floats` | Floating-point output with tolerance | Compares numeric tokens with either absolute or relative error. Set `precision` to the required number of decimal digits. Non-numeric tokens must match exactly. |
| `floatsabs` | Floating-point output with absolute error only | Same as `floats`, but only absolute error is accepted. |
| `floatsrel` | Floating-point output with relative error only | Same as `floats`, but only relative error is accepted. |
| `rstripped` | Output where trailing spaces should not matter, but line structure should | Compares each line after removing trailing whitespace. Other whitespace remains meaningful. |
| `sorted` | Output lines can appear in any order | Ignores empty lines, splits each non-empty line into tokens, sorts the lines, then compares. |
| `identical` | Output must match byte-for-byte | Use for strict format checks or binary/text data where whitespace must be exact. |
| `linecount` | Tokens must match on the same line | Ignores extra whitespace within a line, but line boundaries must match. |
| `csv_accuracy`, `csv_rmse`, `csv_mae`, `csv_f1`, `csv_auc`, `csv_logloss` | Kaggle-style CSV submissions | See [Kaggle-style CSV problems](#kaggle-style-csv-problems) below for `checker_args` and scoring details. |

Use **Testlib** or **Testlib (CMS / IOI)** when you already have a `checker.cpp` from Polygon, IOI, CMS, or a similar package. Upload that file in the C++ checker field. For IOI packages, see [Importing IOI tasks](#importing-ioi-tasks).

### 3.2. Custom Checker (C++) {#custom-checker}

Define custom judging logic for problems with multiple valid answers or special output formats.

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

## 4. Interactive (C++) {#interactive}

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

## 5. IOI Signature {#ioi-signature}

Contestants implement a function; the judge links it with your handler. You provide:
- **Header** (`.h`) — function declaration (C/C++ only)
- **Handler** — driver that reads input, calls the function, prints output

**Example.** Input is `t` followed by `t` integers `n`. Contestants implement `solve(int n)` returning `n * 2`.

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

**Student submission:**
```cpp
int solve(int n) {
    return  n * 2;
}
```

### Python {#ioi-signature-python}
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

### Java {#ioi-signature-java}
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

### Importing IOI tasks {#importing-ioi-tasks}

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

## 6. Testcase Validator {#testcase-validator}

A program that confirms each test input matches the problem's constraints. Reads stdin; exit `0` = valid, non-zero = invalid (stderr captured as feedback). Click **"Run Validator"** to check every test.

### C++ {#testcase-validator-cpp}

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

### Python {#testcase-validator-python}

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

## 7. Output-only Problems {#output-only}

Output-only problems don't require solvers to write a runnable program — instead they download the input data, compute the answer locally (with whatever tools they like), and submit just the result file. To configure one, tick **Is output only** in the test data form. The submit page then accepts a `.zip` (single files are auto-zipped client-side) and the chosen checker is applied to its contents.

> **Allowed language.** Make sure to restrict the **Allowed languages** to just `Output` on the **Languages** tab. Otherwise solvers will see other languages in the submit dropdown and submit source code, which the output-only checker can't grade.

> **Distributing the test inputs to solvers.** Files inside the test-data zip are private to the judge — solvers can't see them. To give solvers the inputs they need to compute answers locally (e.g. the test cases for an IOI-style output-only problem, or the training/test CSV for a Kaggle problem), upload them via the **Attachments** tab on the problem edit page. Attachments appear in a "Files" section on the problem statement page, with download links scoped to the problem's normal access permissions.

### 7.1. Traditional output-only (IOI-style) {#traditional-output-only}

For each test case, name the expected output file in the **Output file** column (e.g. `test01.out`). The submitter's zip must contain a file with the matching name; the configured checker (typically `Standard`, `Floats`, or a custom one) is then applied to compare submission output vs. expected output, the same as for a normal problem.

This format is appropriate when the answer is a single deterministic file per test case (e.g. shortest-path lengths, integer answers, sorted lists). Pick whichever standard or custom checker fits the output type.

### 7.2. Kaggle-style CSV problems {#kaggle-style-csv-problems}

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
