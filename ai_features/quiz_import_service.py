"""
Quiz Import Service — AI-powered question extraction from uploaded files.
Uses Gemini 3 Flash via Poe API to analyze PDF/image/Word documents
and extract structured quiz questions in JSON format.
"""

import json
import logging
import re
from typing import Dict, Any

logger = logging.getLogger(__name__)

VALID_QUESTION_TYPES = {"MC", "MA", "TF", "SA", "ES"}

QUIZ_IMPORT_SYSTEM_PROMPT = """You are an expert at extracting quiz questions from educational documents.
Your task is to analyze the attached document and extract ALL questions as structured JSON.

QUESTION TYPE DETECTION:
- MC (Multiple Choice): Single correct answer from choices
- MA (Multiple Answer): Multiple correct answers from choices
- TF (True/False): Statement that is either true or false
- SA (Short Answer): Requires a brief text/number answer
- ES (Essay): Requires a long-form written response

OUTPUT FORMAT — Return ONLY a JSON object (no markdown fences around the JSON itself):
{
  "questions": [
    {
      "title": "Brief title (max 255 chars)",
      "question_type": "MC",
      "content": "Full question text in markdown",
      "choices": [
        {"id": "A", "text": "First option"},
        {"id": "B", "text": "Second option"},
        {"id": "C", "text": "Third option"},
        {"id": "D", "text": "Fourth option"}
      ],
      "correct_answers": {"answers": "B"}
    }
  ]
}
IMPORTANT: Both "content" and choice "text" fields support full markdown formatting.
If a choice contains code, wrap it in markdown fenced code blocks exactly the same way you would in "content".
Use JSON newline escapes (backslash-n) to preserve line breaks in multi-line code within JSON string values.

CHOICE FORMAT RULES:
- Use UPPERCASE letter IDs: "A", "B", "C", "D", etc.
- For MC: correct_answers = {"answers": "B"} (single letter)
- For MA: correct_answers = {"answers": ["A", "C"]} (array of letters)
- For TF: choices = [{"id": "A", "text": "True"}, {"id": "B", "text": "False"}], correct_answers = {"answers": "A"} or {"answers": "B"}
- For SA: no choices, correct_answers = {"answers": ["exact answer 1", "answer 2"]}
- For ES: no choices, correct_answers = null
- If the correct answer is NOT determinable from the document, set correct_answers to null

CONTENT RULES (CRITICAL — follow exactly):
- Preserve the original language (Vietnamese, English, etc.)
- Use LaTeX math notation: $x^2$, $\\frac{a}{b}$, $\\sum_{i=1}^{n}$
- Use markdown formatting for bold, italic, code blocks, tables
- Extract ALL questions — do not skip any
- NEVER remove, omit, or simplify any information from the original document
- You may improve formatting (e.g., fix markdown syntax, add proper code fences) but MUST keep ALL original content

CODE HANDLING (CRITICAL):
- If the document contains code (in text or as images/screenshots of code), you MUST transcribe it EXACTLY
- Wrap code in markdown fenced code blocks with the language name (e.g., python, cpp, java)
- Preserve exact indentation, variable names, comments, and logic
- CHOICES WITH CODE: When a choice option contains code, the "text" field MUST use fenced code blocks with the language tag, exactly like in "content". Preserve all newlines in multi-line code.
- Do NOT put code as raw unformatted text in choices — always use fenced code blocks so it renders properly
- For images of code: carefully read and transcribe every line — do not summarize or paraphrase code
- For pseudocode: preserve it as-is using fenced code blocks

IMAGE HANDLING:
- If the document contains diagrams, flowcharts, or figures, describe them as [Image: detailed description]
- For images containing code or formulas: transcribe the actual code/formulas instead of describing them
- For images containing tables: convert them to markdown tables

ANSWER HANDLING:
- Extract correct answers exactly as shown in the document
- If the document marks answers (e.g., circled, highlighted, in answer key), use those
- If no answer is marked, you MAY suggest a correct answer if you are confident, but set it
- If you cannot determine the answer at all, set correct_answers to null

TITLE RULES:
- Create a brief, descriptive title from the question content
- Max 255 characters
- Example: "Tìm giá trị x thỏa phương trình bậc 2" or "Calculate area of triangle"
"""

QUIZ_IMPORT_USER_PROMPT = (
    "Analyze the attached document and extract all questions "
    "in the specified JSON format."
)


def parse_quiz_import_response(text: str) -> Dict[str, Any]:
    """Parse the LLM response text into structured question data.

    Handles JSON wrapped in markdown fences, trailing text, etc.
    Returns dict with success, questions list, and summary.
    """
    if not text:
        return {"success": False, "error": "Empty response from AI", "questions": []}

    # Strip markdown code fences if present (handles ```json, ```JSON, ```text, etc.)
    cleaned = text.strip()
    cleaned = re.sub(r"^```\w*\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct JSON parse
    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object in the text
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if not data or not isinstance(data, dict):
        return {
            "success": False,
            "error": "Could not parse AI response as JSON",
            "questions": [],
        }

    questions = data.get("questions", [])
    if not isinstance(questions, list):
        return {
            "success": False,
            "error": "Response missing 'questions' array",
            "questions": [],
        }

    # Validate and normalize each question
    valid_questions = []
    for q in questions:
        if not isinstance(q, dict):
            continue

        qtype = q.get("question_type", "").upper()
        if qtype not in VALID_QUESTION_TYPES:
            continue

        title = str(q.get("title", "")).strip()
        content = str(q.get("content", "")).strip()
        if not content:
            continue

        # Truncate title
        if len(title) > 255:
            title = title[:252] + "..."
        if not title:
            title = content[:80] + ("..." if len(content) > 80 else "")

        # Normalize choices
        choices = q.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and "id" in choice:
                    choice["id"] = str(choice["id"]).upper()
        else:
            choices = None

        # Normalize correct_answers
        correct_answers = q.get("correct_answers")
        if isinstance(correct_answers, dict) and "answers" in correct_answers:
            ans = correct_answers["answers"]
            if isinstance(ans, str):
                correct_answers["answers"] = ans.upper()
            elif isinstance(ans, list):
                correct_answers["answers"] = [
                    a.upper() if isinstance(a, str) and len(a) <= 2 else a for a in ans
                ]
        elif correct_answers is not None:
            correct_answers = None

        valid_questions.append(
            {
                "title": title,
                "question_type": qtype,
                "content": content,
                "choices": choices,
                "correct_answers": correct_answers,
            }
        )

    # Build summary
    type_counts = {}
    has_answers_count = 0
    for q in valid_questions:
        t = q["question_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
        if q["correct_answers"] is not None:
            has_answers_count += 1

    summary = {
        "total_questions": len(valid_questions),
        "type_counts": type_counts,
        "has_answers": has_answers_count,
    }

    return {
        "success": True,
        "questions": valid_questions,
        "summary": summary,
    }
