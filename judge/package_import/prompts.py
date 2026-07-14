"""
Prompt templates for the AI-powered problem package import feature.
Sent to Claude Code on Poe to analyze and convert problem packages.

The prompt is deliberately kept aligned with the `create-problem` skill
(.claude/commands/create-problem.md), which is the ground-truth spec for how
LQDOJ problems actually work (checker types, subtask batching, output-only
knobs). When that skill changes, this prompt should be revisited.
"""

DESCRIPTION_TEMPLATE = """\
Given four natural numbers $a_1, b_1, a_2, b_2$ where $(a_1, b_1)$ are the side lengths of the first rectangle and $(a_2, b_2)$ are the side lengths of the second rectangle. Find the area of the smallest square that can contain both rectangles without overlapping or extending outside.

####Input
- A single line containing four natural numbers $a_1, b_1, a_2, b_2$ $(0 < a_1, b_1, a_2, b_2 \\le 10^6)$.

####Output
- Print a single number — the area of the smallest square satisfying the problem requirements.

####Example

!!! question "Test 1"
    ???+ "Input"
        ```sample
        2 3 2 4
        ```
    ???+ success "Output"
        ```sample
        16
        ```
    ??? warning "Note"
        The two rectangles have dimensions $2 \\cdot 3$ and $2 \\cdot 4$. They fit inside a smallest square of side $4$, so the answer is $16$.

!!! question "Test 2"
    ???+ "Input"
        ```sample
        4 5 4 5
        ```
    ???+ success "Output"
        ```sample
        64
        ```

#### Scoring
 + Subtask $1$ ($40\\%$ points): $a_1, b_1, a_2, b_2 \\le 10^3$.
 + Subtask $2$ ($60\\%$ points): no additional constraints.\
"""

CHECKER_TEMPLATE = """\
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char** argv) {
    ifstream inp(argv[1]);  // test input
    ifstream out(argv[2]);  // contestant output
    ifstream ans(argv[3]);  // expected answer

    // Read expected and contestant answers
    // Compare them

    if (/* correct */) {
        cout << "Correct" << endl;
        return 0;  // AC
    } else {
        cout << "Wrong answer" << endl;
        return 1;  // WA
    }

    // For partial scoring:
    // cerr << 0.5;  // score fraction between 0.0 and 1.0
    // return 2;     // PARTIAL
}\
"""

GENERATOR_TEMPLATE = """\
#include <bits/stdc++.h>
using namespace std;

// === SOLUTION FUNCTION ===
// (Use the accepted solution's logic here to compute the answer)
long long solve(/* parameters */) {
    // TODO: solution logic
    return 0;
}

int main(int argc, char* argv[]) {
    // Parse command-line arguments
    // argv[1], argv[2], ..., argv[argc-2] = constraints/parameters
    // argv[argc-1] = random seed

    int seed = stoi(argv[argc - 1]);
    mt19937 gen(seed);

    // Generate random input based on args
    // ...

    // Print INPUT to stdout
    cout << /* input */ endl;

    // Print ANSWER to stderr (using solve function)
    cerr << solve(/* parameters */) << endl;

    return 0;
}\
"""

INTERACTIVE_TEMPLATE = """\
#include <bits/stdc++.h>
using namespace std;

void quit(string reason) {
    cerr << reason << endl;
    exit(1);  // WA
}

void read_contestant(long long& value) {
    if (!(cin >> value)) exit(1);
}

int main(int argc, char* argv[]) {
    ifstream inp(argv[1]);   // test input
    // ifstream ans(argv[2]); // answer file (if needed)

    // Read test data from input file
    int N;
    inp >> N;

    // Interaction loop
    int guesses = 0;
    while (guesses < MAX_GUESSES) {
        long long guess;
        read_contestant(guess);
        guesses++;

        if (/* correct */) {
            cout << "CORRECT" << endl;
            cerr << "Solved in " << guesses << " queries" << endl;
            return 0;  // AC
        } else {
            cout << /* hint */ << endl;
        }
    }

    cerr << "Too many guesses" << endl;
    return 1;  // WA

    // For partial scoring:
    // cerr << 0.5;  // score fraction between 0.0 and 1.0
    // return 2;     // PARTIAL
}\
"""


IMPORT_PROMPT = """\
I uploaded a competitive programming problem package (zip file).
Extract it, analyze all files, detect the format, and create the output files described below.

=== LQDOJ DESCRIPTION FORMAT ===
Convert the problem statement to this markdown format.

FORMATTING RULES:
1. Use LaTeX math notation with $ for inline math (e.g., $n$, $a_i$, $10^9$)
2. Use #### for section headers (####Input, ####Output, ####Example, ####Scoring)
3. Format constraints in bullet points with - prefix
4. CRITICAL — Test case admonition format with STRICT INDENTATION:
   - !!! question "Test X" ← 0 spaces indent
   -     ???+ "Input" ← 4 spaces indent
   -         ```sample ← 8 spaces indent
   -         line of input ← 8 spaces indent (EVERY line must have 8 spaces!)
   -         ``` ← 8 spaces indent
   -     ???+ success "Output" ← 4 spaces indent
   -         ```sample ← 8 spaces indent
   -         line of output ← 8 spaces indent (EVERY line!)
   -         ``` ← 8 spaces indent
   -     ??? warning "Note" ← 4 spaces indent (optional)
   -         explanation text ← 8 spaces indent
   ALL content lines inside ```sample blocks MUST be indented with exactly 8 spaces.
   If any line is missing indentation, the entire test case display will break.
5. Keep all images as markdown links: ![description](url)
6. Use \\cdot for multiplication in math mode
7. Sublist items MUST have 4 spaces indentation more than their parent
8. Inline LaTeX MUST NOT have spaces after opening $ or before closing $
9. For string literals, use `code` backticks, NOT LaTeX
10. Convert any LaTeX (\\begin{{itemize}}, \\textbf{{}}, etc.) to standard markdown
11. Preserve the original language (Vietnamese or English)

HERE IS A COMPLETE EXAMPLE of the target format:

{description_template}

=== LQDOJ CHECKER SELECTION ===
LQDOJ has built-in checkers. ALWAYS PREFER a built-in checker over writing a
custom one — a built-in is more robust and needs no compilation. In summary.json,
report the choice as a `checker` object: {{"key": <one below>, "args": {{...}}, "source_file": <file or omit>}}.

Available checker keys:
  - standard   : token match, ignores surrounding whitespace. Default for most problems. No file.
  - floats     : floating-point compare with tolerance. args {{"precision": N}} (N decimal places). No file.
  - floatsabs  : floats, absolute error only. args {{"precision": N}}. No file.
  - floatsrel  : floats, relative error only. args {{"precision": N}}. No file.
  - identical  : byte-identical output. No file.
  - rstripped  : token match ignoring trailing spaces. No file.
  - sorted     : compare as an unordered multiset of tokens/lines. No file.
  - linecount  : line-by-line comparison. No file.
  - testlib    : KEEP an existing testlib.h checker AS-IS. Return it unchanged as checker.cpp, source_file="checker.cpp".
  - testlibcms : a testlib checker using CMS/IOI-style scoring (registerTestlibCmd + quitp, or prints score to stdout). Return checker.cpp, source_file="checker.cpp".
  - customcpp  : a bespoke C++ checker with NO testlib dependency (see template). Return checker.cpp, source_file="checker.cpp".
  - custom     : a Python checker (checker.py). Return checker.py, source_file="checker.py".
  - interact   : interactive judge, plain protocol (see below). Return interactive.cpp, source_file="interactive.cpp".
  - interacttl : interactive judge that uses testlib. Return interactive.cpp, source_file="interactive.cpp".

Polygon / Codeforces checker name → LQDOJ key mapping:
  wcmp, lcmp, yesno, nyesno            → standard
  ncmp, rcmp4, rcmp6, rcmp9, rcmpXX    → floats  (set "precision" to match the checker)
  fcmp                                 → identical
  Any testlib checker that calls quitp / reports partial scores → testlibcms
  Any OTHER testlib checker            → testlib   (keep the source AS-IS)
  A non-testlib custom comparator      → customcpp (rewrite to plain ifstream) or custom (Python)

CRITICAL: Do NOT rewrite a testlib checker into ifstream. LQDOJ compiles testlib
checkers natively (testlib.h is installed on the judge), and rewriting silently
loses robust tokenizing and partial-scoring semantics. Only produce a customcpp
or custom checker when the original genuinely does NOT use testlib.

=== LQDOJ CUSTOM CHECKER FORMAT (C++, key=customcpp) ===
Only when the checker does not use testlib. Takes 3 file arguments: input_file, output_file, ans_file.
Open them with ifstream. Exit code: 0=AC, 1=WA, 2=partial.
For partial: print a score fraction (0.0 to 1.0) to stderr, return 2.
Print feedback to stdout (shown to submitter).

Template:
```cpp
{checker_template}
```

=== LQDOJ GENERATOR FORMAT (C++) ===
Takes command-line arguments (each line of generator script = one test's args).
Print INPUT to stdout (cout).
Print ANSWER to stderr (cerr).
Must include solution logic to compute the correct answer.
If there is an accepted solution in the package, use its logic for stderr output.

Template:
```cpp
{generator_template}
```

=== LQDOJ INTERACTIVE JUDGE FORMAT (C++, key=interact) ===
Takes 2 file arguments: input_file, answer_file
Communicate with contestant via stdin (read from contestant) / stdout (write to contestant).
Return exit code: 0=AC, 1=WA, 2=partial.
Print feedback to stderr.

Template:
```cpp
{interactive_template}
```

=== TEST STRUCTURE (subtasks / scoring) ===
Describe how the tests are organized in summary.json's `test_structure` so LQDOJ
can create the graded test cases and regenerate init.yml. Use EXACT file basenames
as they appear inside testdata.zip.

"test_structure": {{
  "kind": "flat",              // "flat" (independent cases) OR "batched" (subtasks)

  // kind=flat — a simple list of independent test cases:
  "cases": [
    {{"input": "01.in", "output": "01.out", "points": 10, "is_pretest": false}}
  ],

  // kind=batched — one entry per subtask/group (each graded as a batch):
  "subtasks": [
    {{
      "points": 40,
      "scoring": "each_test",  // "each_test" OR "all_or_nothing"
      "cases": [ {{"input": "1-01.in", "output": "1-01.out"}} ]
    }}
  ]
}}

Rules:
- Use "batched" when the package defines subtasks/groups (Polygon <group>, IOI
  subtasks, problem.yaml `grading`/`limits`). Otherwise use "flat".
- Per-subtask `scoring`: DERIVE from the package when stated:
    * Polygon problem.xml points_policy: "complete-group" → "all_or_nothing";
      "each-test" → "each_test".
    * Kattis testdata.yaml grader_flags: "min" or "first_error" → "all_or_nothing";
      "sum" / "avg" / "accept_if_any_accepted" → "each_test".
  When the policy is unknown, use "each_test".
- `points`: use the package's per-subtask (batched) or per-case (flat) points when
  available. For Kattis subtasks, take the subtask points from testdata.yaml
  `accept_score`, or the upper bound of `range` (e.g. "range: 0 50" → 50). If not
  stated, omit points and LQDOJ will distribute them evenly.
- `is_pretest`: mark sample/example cases as pretests (flat only; batched pretests
  are uncommon).
- EVERY referenced input/output MUST be a file present in testdata.zip. Do not
  invent names. If a case has no separate output file (interactive/output-only),
  set "output" to "" or the answer file name.
- For output-only / single-answer problems, use kind=flat with one case whose
  `output` is the answer file's name.

=== INSTRUCTIONS ===

1. DETECT FORMAT: Check for problem.xml (Polygon), problem.yaml (Kattis/ICPC),
   or infer from file structure. Support any format.

2. CREATE THESE OUTPUT FILES:

   a) description.md — Problem statement converted to LQDOJ format (see template above).
      - Use $...$ for inline math, $$...$$ for display math
      - Convert LaTeX commands to markdown
      - Include ALL examples using the !!! question admonition format
      - Include scoring/subtask info if available

   b) testdata.zip — Zip the test data folder using a SHELL COMMAND:
        cd <parent_dir> && zip -r /mnt/testdata.zip <test_folder>/
      Do NOT use Python's zipfile module to read and re-write files.
      Do NOT rename or modify any test files.
      LQDOJ supports these extensions: .in, .inp, .out, .ans, .a, and extensionless files.
      Just zip the test folder as-is. The exact basenames you put here must match
      the names you list in test_structure.

   c) checker.cpp / checker.py — ONLY when checker.key needs a file (customcpp,
      custom, testlib, testlibcms). For testlib/testlibcms, return the ORIGINAL
      source unchanged. For customcpp, rewrite to plain ifstream (see template).
      For a built-in checker (standard/floats/etc.) do NOT create this file.

   d) generator.cpp — Convert to LQDOJ format (see template above).
      Must print input to stdout AND answer to stderr.
      Use an accepted solution's logic (from the package) to compute the answer.
      If no generator exists, do not create this file.

   e) generator_script.txt — Extract test generation commands from metadata.
      One line per generated test case, just the arguments (strip generator name prefix).
      For Polygon: extract <test method="generated" cmd="gen X Y Z"/> → "X Y Z"
      Skip manual/sample tests. If no generator commands found, do not create this file.

   f) interactive.cpp — If this is an interactive problem, convert the interactor
      to LQDOJ format (see template above), and set checker.key to "interact" (plain)
      or "interacttl" (testlib). If not interactive, do not create this file.

   g) If the problem statement references images (e.g., diagrams, figures),
      find those image files in the package and send them back as attachments too.
      Common locations: statement-sections/, img/, images/, or alongside the .tex file.

   h) For each solution found in the package, create a file named:
      sol_{{verdict}}_{{name}}.{{ext}}
      Examples: sol_ac_main.cpp, sol_wa_brute.cpp, sol_tle_slow.py
      Determine verdict from: filename, .desc files, directory structure, or annotations.

   i) summary.json — Metadata about the package:
      {{
        "format": "polygon|kattis|usaco|cms|simple|kaggle|output_only|unknown",
        "problem_name": "name or null",
        "time_limit_seconds": number or null,
        "memory_limit_mb": number or null,
        "checker": {{
            "key": "standard|floats|floatsabs|floatsrel|identical|rstripped|sorted|linecount|customcpp|custom|testlib|testlibcms|interact|interacttl",
            "args": {{}} or {{"precision": 6}},
            "source_file": "checker.cpp" (only for file-bearing keys; omit otherwise)
        }},
        "test_count": total number of test input files,
        "sample_count": number of sample/example tests,
        "has_generator": true/false,
        "has_checker": true/false,
        "has_interactive": true/false,
        "output_only": true/false,
        "binary_data": true/false,
        "output_zip_size_mb": null OR integer (max 20),
        "test_structure": {{ ...see TEST STRUCTURE section above... }},
        "csv_checker": null OR {{
            "metric": "csv_accuracy|csv_rmse|csv_mae|csv_f1|csv_auc|csv_logloss",
            "id_column": "name or empty string",
            "label_column": "name or empty string",
            "has_header": true/false,
            "baseline": null OR positive number (lower-better only)
        }},
        "attachments": [
            {{"name": "train.csv", "description": "Training data"}}
        ],
        "solutions": [
          {{"name": "main.cpp", "language": "cpp", "verdict": "AC"}}
        ],
        "notes": "any observations about the package"
      }}

   j) Attachment files (Kaggle / output-only problems): if the package distributes
      input files to solvers (e.g. train.csv, sample_submission.csv, dataset zips,
      reference PDFs, sample images), include each as a returned attachment AND
      list it in summary.json's `attachments` array with a short description.
      Do NOT include answer keys or hidden test inputs as attachments — those go
      in testdata.zip only.

=== OUTPUT-ONLY DETECTION ===
Set `output_only: true` when ANY of these hold:
- init.yml or problem.yaml has `output_only: true`
- The package contains expected output files but no required submission program
- The problem statement says "submit the output / predictions / answer file"

For output-only problems:
- Set `binary_data: true` when the expected answer files are BINARY (.npz, .npy,
  images, serialized tensors). Otherwise the judge normalizes newlines/EOF and
  corrupts them (np.load raises "negative seek value").
- Set `output_zip_size_mb` to an integer (max 20) when a contestant's submitted
  output can exceed 1 MB; otherwise leave it null.
- With traditional file-by-file scoring (IOI-style), keep `csv_checker: null` and
  let the chosen built-in / custom checker apply.

=== KAGGLE-STYLE CSV DETECTION ===
Set `csv_checker` (and `output_only: true`) when the problem expects a CSV
of predictions scored against a hidden answer:
- The package contains `train.csv`/`test.csv`/`sample_submission.csv` style files
- The problem statement mentions a metric like accuracy/RMSE/MAE/F1/AUC/log-loss
- Solvers submit a single CSV of predictions

Pick the metric from the statement. Choose `id_column`/`label_column` from
sample_submission.csv columns when available; if the file has only one column
(predictions only) leave both empty so the checker aligns by row index.

3. IMPORTANT — SEND ALL FILES BACK:
   After creating all files, you MUST attach/send every output file back to me.
   Send each file as an attachment. This is critical — I cannot access your filesystem.
   Files to send: description.md, testdata.zip, checker.cpp (or checker.py),
   generator.cpp, generator_script.txt, interactive.cpp, summary.json, and all
   sol_*.* files. Only send files that you actually created (skip ones that don't exist).

4. VERIFY before sending:
   - testdata.zip contains test files (check file count)
   - every input/output listed in test_structure exists inside testdata.zip
   - checker.cpp compiles if created (try: g++ -o /dev/null checker.cpp 2>&1)
   - description.md uses correct admonition format for examples\
"""


def build_import_prompt(hint: str = ""):
    """Build the full prompt for Claude Code with all templates filled in.
    `hint`: optional free-text from the author (e.g. "Kaggle-like, train/test 80/20,
    metric is RMSE on column y") prepended to the prompt."""
    body = IMPORT_PROMPT.format(
        description_template=DESCRIPTION_TEMPLATE,
        checker_template=CHECKER_TEMPLATE,
        generator_template=GENERATOR_TEMPLATE,
        interactive_template=INTERACTIVE_TEMPLATE,
    )
    if hint:
        body = f"=== AUTHOR NOTES (use these as guidance) ===\n{hint}\n\n" + body
    return body
