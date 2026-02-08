"""
Chatbot tools for problem author assistance.
Each tool fetches specific information about a problem.
"""

import os
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

CHATBOT_TOOLS = {
    "get_problem_info": {
        "description": "Get basic problem metadata (name, code, points, time/memory limits, authors)"
    },
    "get_problem_statement": {
        "description": "Get the problem description/statement text"
    },
    "get_test_data_docs": {
        "description": "Get documentation for test data management (generators, checkers, interactive)"
    },
    "get_checker_template": {
        "description": "Get template code for writing custom checkers (Python and C++)"
    },
    "get_generator_template": {
        "description": "Get template code for writing test generators"
    },
    "get_ac_submissions": {"description": "Get accepted submission code for reference"},
    "get_existing_checker": {
        "description": "Get the current checker code if one exists"
    },
    "get_solution_template": {
        "description": "Get the template format for writing problem solutions/editorials"
    },
}


def execute_tool(tool_name, problem):
    """Execute a tool and return the result."""
    tool_functions = {
        "get_problem_info": _get_problem_info,
        "get_problem_statement": _get_problem_statement,
        "get_test_data_docs": _get_test_data_docs,
        "get_checker_template": _get_checker_template,
        "get_generator_template": _get_generator_template,
        "get_ac_submissions": _get_ac_submissions,
        "get_existing_checker": _get_existing_checker,
        "get_solution_template": _get_solution_template,
    }

    if tool_name in tool_functions:
        try:
            return tool_functions[tool_name](problem)
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}")
            return f"Error executing {tool_name}: {str(e)}"
    return f"Unknown tool: {tool_name}"


def _get_problem_info(problem):
    """Get basic problem metadata."""
    authors = ", ".join([a.user.username for a in problem.authors.all()]) or "None"
    curators = ", ".join([c.user.username for c in problem.curators.all()]) or "None"
    types = ", ".join([t.full_name for t in problem.types.all()]) or "None"
    languages = (
        ", ".join([l.name for l in problem.allowed_languages.all()[:10]]) or "All"
    )
    if problem.allowed_languages.count() > 10:
        languages += f" (+{problem.allowed_languages.count() - 10} more)"

    return f"""Problem Information:
- Code: {problem.code}
- Name: {problem.name}
- Points: {problem.points}
- Time Limit: {problem.time_limit}s
- Memory Limit: {problem.memory_limit} KB
- Authors: {authors}
- Curators: {curators}
- Types: {types}
- Is Public: {problem.is_public}
- Partial Points: {problem.partial}
- Allowed Languages: {languages}"""


def _get_problem_statement(problem):
    """Get the problem description."""
    if problem.description:
        # Limit to 8000 chars for context window
        desc = problem.description
        if len(desc) > 8000:
            desc = desc[:8000] + "\n\n... (truncated)"
        return f"Problem Statement:\n{desc}"
    return "No problem statement found. The problem may use a PDF description."


def _get_test_data_docs(problem):
    """Get test data documentation."""
    docs_path = os.path.join(
        settings.BASE_DIR, "templates", "docs", "test-data-instructions.en.md"
    )
    try:
        with open(docs_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Limit to first 8000 chars
        if len(content) > 8000:
            content = content[:8000] + "\n\n... (truncated)"
        return f"Test Data Documentation:\n{content}"
    except FileNotFoundError:
        return "Test data documentation file not found."
    except Exception as e:
        return f"Error reading test data documentation: {str(e)}"


def _get_checker_template(problem):
    """Get checker templates."""
    python_template = '''"""Custom Python Checker Template"""
from dmoj.result import CheckerResult

def wa(feedback):
    """Return Wrong Answer with feedback."""
    return CheckerResult(False, 0, feedback)

def check(process_output, judge_output, judge_input, **kwargs):
    """
    Main checker function.

    Available parameters:
    - process_output: contestant's output (string)
    - judge_output: expected answer (string)
    - judge_input: input data (string)
    - point_value: points for current test case
    - submission_source: submission source code
    - execution_time: execution time in seconds

    Return:
    - True: Accepted (full points)
    - False: Wrong Answer (0 points)
    - CheckerResult(passed, points, feedback): Custom result
      - passed: bool (AC or WA)
      - points: float (0 to point_value)
      - feedback: string (shown to user)
    """
    # Parse input if needed
    # input_lines = judge_input.strip().split("\\n")

    # Parse outputs
    output_lines = process_output.strip().split("\\n")
    expected_lines = judge_output.strip().split("\\n")

    # Example: exact match check
    if output_lines == expected_lines:
        return True

    return wa("Output does not match expected answer")


# Example: Partial scoring checker
def check_partial(process_output, judge_output, judge_input, point_value, **kwargs):
    """Example checker with partial scoring."""
    try:
        user_ans = int(process_output.strip())
        expected = int(judge_output.strip())

        if user_ans == expected:
            return True

        # Give partial points based on closeness
        diff = abs(user_ans - expected)
        if diff <= 10:
            return CheckerResult(False, point_value * 0.5, f"Close! Difference: {diff}")

        return wa(f"Wrong answer. Expected {expected}, got {user_ans}")
    except ValueError:
        return wa("Invalid output format")
'''

    cpp_template = """// Custom C++ Checker Template (testlib style)
#include "testlib.h"
#include <bits/stdc++.h>
using namespace std;

int main(int argc, char* argv[]) {
    // Register checker with testlib
    registerTestlibCmd(argc, argv);

    // Read from streams:
    // inf - input file
    // ouf - contestant output
    // ans - expected answer

    // Example: Read integers
    // int expected = ans.readInt();
    // int contestant = ouf.readInt();

    // Example: Read line
    // string expected_line = ans.readLine();
    // string contestant_line = ouf.readLine();

    // Verdict functions:
    // quitf(_ok, "message");     // Accepted
    // quitf(_wa, "message");     // Wrong Answer
    // quitf(_pe, "message");     // Presentation Error
    // quitf(_pc(score), "msg");  // Partial (score 0-100)

    // Example checker:
    int n = inf.readInt();

    for (int i = 0; i < n; i++) {
        int expected = ans.readInt();
        int contestant = ouf.readInt();

        if (expected != contestant) {
            quitf(_wa, "Mismatch at position %d: expected %d, got %d",
                  i + 1, expected, contestant);
        }
    }

    quitf(_ok, "All %d values match", n);
    return 0;
}

/*
Compile with: g++ -o checker checker.cpp -I/path/to/testlib
Run with: ./checker input.txt output.txt answer.txt
*/
"""

    return f"""Python Checker Template:
```python
{python_template}
```

C++ Checker Template (Testlib):
```cpp
{cpp_template}
```

Notes:
- Python checkers are simpler but slower
- C++ checkers with testlib are faster and recommended for large tests
- For interactive problems, use interactive judge instead"""


def _get_generator_template(problem):
    """Get generator template."""
    template = """// Competitive Programming Test Generator
#include <bits/stdc++.h>
using namespace std;

mt19937_64 rng;

// ========== SOLUTION FUNCTION ==========
long long solve(/* input parameters */) {
    // TODO: Implement the actual solution logic here
    return 0;
}

// ========== GENERATOR MODES ==========
void generate_random(long long max_n) {
    // Random case within constraints
    long long n = uniform_int_distribution<long long>(1, max_n)(rng);
    cout << n << endl;
    cerr << solve(/* n */) << endl;
}

void generate_edge_min() {
    // Edge case: minimum values
    cout << 1 << endl;
    cerr << solve(/* 1 */) << endl;
}

void generate_edge_max(long long max_n) {
    // Edge case: maximum value (exact, not random)
    cout << max_n << endl;
    cerr << solve(/* max_n */) << endl;
}

int main(int argc, char* argv[]) {
    // Usage: ./gen <mode> <arg1> [arg2] <seed>
    // Modes: random, min, max, special1, special2, ...

    if (argc < 3) {
        cerr << "Usage: ./gen <mode> <args...> <seed>" << endl;
        return 1;
    }

    string mode = argv[1];
    int seed = stoi(argv[argc - 1]);
    rng.seed(seed);

    if (mode == "random") {
        long long max_n = stoll(argv[2]);
        generate_random(max_n);
    } else if (mode == "min") {
        generate_edge_min();
    } else if (mode == "max") {
        long long max_n = stoll(argv[2]);
        generate_edge_max(max_n);
    }
    // Add more modes for problem-specific cases

    return 0;
}
"""

    return f"""
Generator Template:
```cpp
{template}
```

===============================================================================
**HOW TO USE GENERATORS ON LQDOJ**
===============================================================================

**Step-by-step workflow:**
1. Go to **Test Data** page (sidebar → "Test Data")
2. In the **Generator** section, paste the C++ generator code above
3. In the **Generator Script** field, write the test generation commands (see below)
4. Click **"Fill testcases"** to generate all test files
5. Click **"Apply!"** to save the changes

===============================================================================
**COMPREHENSIVE TEST GENERATION FOR COMPETITIVE PROGRAMMING**
===============================================================================

Generate tests that thoroughly cover all scenarios. A good test suite includes:

**1. EDGE CASES (Exact values, deterministic):**
- Minimum input (n=1, empty cases)
- Maximum input (n=max constraint)
- Boundary values (powers of 2, off-by-one)
- Special values (0, -1, primes)

**2. PROBLEM-SPECIFIC CASES:**
- **Trees:** line/chain, star, binary tree, complete tree, random
- **Graphs:** complete, sparse, dense, disconnected, with cycles
- **Arrays:** sorted, reverse sorted, all same, alternating, random
- **Strings:** single char repeated, palindrome, alternating chars

**3. RANDOM CASES:** Various sizes within each subtask constraint

**Example Generator Script (paste into Generator Script field):**
```
# Edge cases
min 1001
max 1000000 1002

# Subtask 1: n <= 100 (20%)
random 100 2001
random 100 2002

# Subtask 2: n <= 10^4 (30%)
random 10000 3001
random 10000 3002
random 10000 3003

# Subtask 3: n <= 10^6 (50%)
random 1000000 4001
random 1000000 4002
random 1000000 4003
random 1000000 4004
random 1000000 4005
```

**For Tree Problems:**
```
# Edge cases
min 1001
line 100 1002
star 100 1003
binary 100 1004

# Random trees
random 10000 2001
random 100000 3001
```

**Generator Script Format Rules:**
- Each non-comment line = one test case
- Format: `<mode> <args...> <seed>` (space-separated)
- **Comment lines:** Lines starting with `#` or `//` are skipped
- **IMPORTANT:** Do NOT use inline comments (e.g., `random 100 1001 // test 1` will FAIL)
- Match test count to subtask percentages from problem statement
- Use different seeds for variety
- Include BOTH deterministic edge cases AND random cases"""


def _get_ac_submissions(problem):
    """Get accepted submissions for reference."""
    from judge.models import Submission

    submissions = []

    # Try author submissions first
    for author in problem.authors.all()[:3]:
        sub = (
            Submission.objects.filter(problem=problem, user=author, result="AC")
            .order_by("-date")
            .first()
        )
        if sub:
            submissions.append(sub)

    # If no author submissions, get any AC
    if not submissions:
        submissions = list(
            Submission.objects.filter(problem=problem, result="AC").order_by("-date")[
                :3
            ]
        )

    if not submissions:
        return "No accepted submissions found for this problem."

    results = []
    for sub in submissions[:2]:  # Limit to 2 submissions
        try:
            source = sub.source.source
            if len(source) > 4000:
                source = source[:4000] + "\n... (truncated)"
            lang = sub.language.name if sub.language else "Unknown"
            user = sub.user.user.username if sub.user else "Unknown"
            results.append(
                f"### {lang} by {user}\n```{lang.lower().split()[0]}\n{source}\n```"
            )
        except Exception as e:
            logger.error(f"Error reading submission source: {e}")
            continue

    if results:
        return "Accepted Submissions:\n\n" + "\n\n".join(results)
    return "Could not retrieve submission source code."


def _get_existing_checker(problem):
    """Get the current checker code."""
    try:
        if not hasattr(problem, "data_files"):
            return "No test data configured for this problem."

        data = problem.data_files
        checker_type = data.checker or "standard"

        result = f"Current checker type: {checker_type}\n"

        # Get Python checker
        if data.custom_checker and data.custom_checker.name:
            try:
                data.custom_checker.open("r")
                code = data.custom_checker.read().decode("utf-8")
                if len(code) > 4000:
                    code = code[:4000] + "\n... (truncated)"
                result += f"\nPython Checker Code:\n```python\n{code}\n```"
                data.custom_checker.close()
            except Exception as e:
                result += f"\n(Could not read Python checker file: {e})"

        # Get C++ checker
        if data.custom_checker_cpp and data.custom_checker_cpp.name:
            try:
                data.custom_checker_cpp.open("r")
                code = data.custom_checker_cpp.read().decode("utf-8")
                if len(code) > 4000:
                    code = code[:4000] + "\n... (truncated)"
                result += f"\nC++ Checker Code:\n```cpp\n{code}\n```"
                data.custom_checker_cpp.close()
            except Exception as e:
                result += f"\n(Could not read C++ checker file: {e})"

        # Get generator if exists
        if data.generator and data.generator.name:
            try:
                data.generator.open("r")
                code = data.generator.read().decode("utf-8")
                if len(code) > 2000:
                    code = code[:2000] + "\n... (truncated)"
                result += f"\nGenerator Code:\n```cpp\n{code}\n```"
                data.generator.close()
            except Exception as e:
                result += f"\n(Could not read generator file: {e})"

        if data.generator_script:
            script = data.generator_script
            if len(script) > 500:
                script = script[:500] + "\n... (truncated)"
            result += f"\nGenerator Script:\n```\n{script}\n```"

        return (
            result
            if "Checker Code" in result or "Generator" in result
            else "No custom checker or generator configured. Using standard checker."
        )

    except Exception as e:
        logger.error(f"Error getting existing checker: {e}")
        return f"Error retrieving checker data: {str(e)}"


def _get_solution_template(problem):
    """Get the solution editorial template."""
    template = """## Tóm tắt đề bài
[Mô tả ngắn gọn yêu cầu bài toán]

## Phân tích
- **Ràng buộc:** [Nêu các ràng buộc quan trọng]
- **Độ phức tạp mục tiêu:** O(...)

## Hướng giải quyết

### Nhận xét
1. [Nhận xét quan trọng 1]
2. [Nhận xét quan trọng 2]

### Thuật toán
1. [Bước 1]
2. [Bước 2]
3. [Bước 3]

## Độ phức tạp
- **Thời gian:** O(...)
- **Bộ nhớ:** O(...)

## Code tham khảo

```cpp
#include <bits/stdc++.h>
using namespace std;

int main() {
    ios_base::sync_with_stdio(false);
    cin.tie(NULL);

    // Your solution here

    return 0;
}
```

## Ghi chú thêm
[Các lưu ý, cách tiếp cận khác, edge cases cần xử lý]
"""

    return f"""Solution/Editorial Template (Vietnamese):
```markdown
{template}
```

Guidelines:
- Keep explanations clear and concise
- Include complexity analysis
- Provide working reference code
- Mention edge cases and common mistakes"""
