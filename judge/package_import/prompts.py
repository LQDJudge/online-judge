"""
Prompt templates for the AI-powered problem package import feature.
Sent to Claude Code on Poe to analyze and convert problem packages.
"""

DESCRIPTION_TEMPLATE = """\
Given four natural numbers $a_1, b_1, a_2, b_2$ where $(a_1, b_1)$ are the side lengths of the first rectangle and $(a_2, b_2)$ are the side lengths of the second rectangle. Find the area of the smallest square that can contain both rectangles without overlapping or extending outside.

####Input
- Input consists of four lines, each containing a natural number $a_1, b_1, a_2, b_2 (0 < a_1, b_1, a_2, b_2\\leq 10^6)$.

####Output
- Print a single number — the area of the smallest square satisfying the problem requirements.

####Example

!!! question "Test 1"
    ???+ "Input"
        ```sample
        4
        2 3
        2 4
        ```
    ???+ success "Output"
        ```sample
        16
        ```
    ??? warning "Note"
        The two rectangles have dimensions $2\\cdot 3$ and $2\\cdot 4$. They fit inside a smallest square of size $4\\cdot 4$. So the answer is $16$.

!!! question "Test 2"
    ???+ "Input"
        ```sample
        4
        4 5
        4 5
        ```
    ???+ success "Output"
        ```sample
        64
        ```

#### Scoring
 + Subtask $1$ ($20\\%$ points): $n, k, x, |A_i|$ $\\le 10^3$
 + Subtask $2$ ($20\\%$ points): $n, k \\le 2 \\cdot 10^5, |A_i|, x \\le 10^9$
 + Subtask $3$ ($60\\%$ points): no additional constraints.\
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
    // cerr << 0.5;  // score between 0.0 and 1.0
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
    // cerr << 0.5;  // score between 0.0 and 1.0
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

=== LQDOJ CHECKER FORMAT (C++) ===
Takes 3 file arguments: input_file, output_file, ans_file
Open them with ifstream. Return exit code: 0=AC, 1=WA, 2=partial.
For partial: print a score (0.0 to 1.0) to stderr, return 2.
Print feedback to stdout (shown to submitter).
Do NOT use testlib.h — use plain ifstream.

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

=== LQDOJ INTERACTIVE JUDGE FORMAT (C++) ===
Takes 2 file arguments: input_file, answer_file
Communicate with contestant via stdin (read from contestant) / stdout (write to contestant).
Return exit code: 0=AC, 1=WA, 2=partial.
Print feedback to stderr.

Template:
```cpp
{interactive_template}
```

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
      Just zip the test folder as-is.

   c) checker.cpp — Convert the checker to LQDOJ format (see template above).
      Rewrite using ifstream(argv[1/2/3]) instead of testlib.h.
      Preserve the exact comparison logic from the original.
      If no custom checker exists, do not create this file.

   d) generator.cpp — Convert to LQDOJ format (see template above).
      Must print input to stdout AND answer to stderr.
      Use an accepted solution's logic (from the package) to compute the answer.
      If no generator exists, do not create this file.

   e) generator_script.txt — Extract test generation commands from metadata.
      One line per generated test case, just the arguments (strip generator name prefix).
      For Polygon: extract <test method="generated" cmd="gen X Y Z"/> → "X Y Z"
      Skip manual/sample tests. If no generator commands found, do not create this file.

   f) interactive.cpp — If this is an interactive problem, convert the interactor
      to LQDOJ format (see template above). If not interactive, do not create this file.

   g) If the problem statement references images (e.g., diagrams, figures),
      find those image files in the package and send them back as attachments too.
      Common locations: statement-sections/, img/, images/, or alongside the .tex file.

   h) For each solution found in the package, create a file named:
      sol_{{verdict}}_{{name}}.{{ext}}
      Examples: sol_ac_main.cpp, sol_wa_brute.cpp, sol_tle_slow.py
      Determine verdict from: filename, .desc files, directory structure, or annotations.

   i) summary.json — Metadata about the package:
      {{
        "format": "polygon|kattis|usaco|cms|simple|unknown",
        "problem_name": "name or null",
        "time_limit_seconds": number or null,
        "memory_limit_mb": number or null,
        "checker_type": "standard|custom|interactive|null",
        "test_count": total number of test input files,
        "sample_count": number of sample/example tests,
        "has_generator": true/false,
        "has_checker": true/false,
        "has_interactive": true/false,
        "solutions": [
          {{"name": "main.cpp", "language": "cpp", "verdict": "AC"}}
        ],
        "notes": "any observations about the package"
      }}

3. IMPORTANT — SEND ALL FILES BACK:
   After creating all files, you MUST attach/send every output file back to me.
   Send each file as an attachment. This is critical — I cannot access your filesystem.
   Files to send: description.md, testdata.zip, checker.cpp, generator.cpp,
   generator_script.txt, interactive.cpp, summary.json, and all sol_*.* files.
   Only send files that you actually created (skip ones that don't exist).

4. VERIFY before sending:
   - testdata.zip contains test files (check file count)
   - checker.cpp compiles if created (try: g++ -o /dev/null checker.cpp 2>&1)
   - description.md uses correct admonition format for examples\
"""


def build_import_prompt():
    """Build the full prompt for Claude Code with all templates filled in."""
    return IMPORT_PROMPT.format(
        description_template=DESCRIPTION_TEMPLATE,
        checker_template=CHECKER_TEMPLATE,
        generator_template=GENERATOR_TEMPLATE,
        interactive_template=INTERACTIVE_TEMPLATE,
    )
