"""
Quiz Import Service — AI-powered question extraction from uploaded files.
Uses Gemini 3 Flash via Poe API to analyze PDF/image/Word documents
and extract structured quiz questions in JSON format.
"""

import json
import logging
import re
from typing import Any, Dict

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
- Use UPPERCASE letter IDs for choices: "A", "B", "C", "D", etc.
- MC/MA/TF have choices; SA and ES have no choices.
- See ANSWER SEMANTICS below for exactly what to put in correct_answers per type.

ANSWER SEMANTICS — how our system interprets correct_answers (READ CAREFULLY):
The meaning of correct_answers is DIFFERENT for each question type. Emitting the
wrong shape or wrong meaning causes silent grading bugs, so follow each rule exactly.

- MC (single choice): {"answers": "B"}
  - "answers" is ONE choice id, and it MUST be one of the ids you listed in "choices".
  - Exactly one correct choice.

- MA (multiple answers): {"answers": ["A", "C"]}
  - "answers" is the COMPLETE set of choice ids that are correct TOGETHER (logical AND).
  - The student must select exactly this set. List every correct id and no wrong ones.
    Every id must appear in "choices".

- TF (true/false): choices = [{"id": "A", "text": "True"}, {"id": "B", "text": "False"}];
  {"answers": "A"} for true, {"answers": "B"} for false.

- SA (short answer): {"type": "exact", "case_sensitive": false, "answers": ["<answer>", ...]}
  - CRITICAL: the "answers" list is a set of ALTERNATIVE, EQUIVALENT answers. The student
    types ONE answer and is graded correct if it matches ANY ONE entry (logical OR).
  - The list is NOT the parts of a single answer. If the correct answer has multiple parts
    (e.g. the ages of 4 people), it is ONE entry containing the WHOLE answer:
        RIGHT: {"answers": ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"]}
        WRONG: {"answers": ["Chloe: 5", "Leo: 8", "Emma: 13", "Lily: 15"]}
  - Add more than one entry ONLY when the document explicitly gives equivalent forms
    (e.g. an answer key that says "5 or five" -> ["5", "five"]).
  - ANSWER FORMAT INSTRUCTIONS: if the question tells students how to write the answer
    (e.g. "Hướng dẫn ghi đáp án", "write in the format: ..."), the single accepted
    answer MUST follow that exact format.
  - SA answers are always graded case-insensitively with exact match - write answers that
    grade correctly under that rule (do not rely on capitalization).

- ES (essay): correct_answers = null (manually graded).

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
- Create a SHORT, NEUTRAL, THEMATIC title from the question's story or setting.
- The title MUST NOT reveal or hint at the solution, the method/algorithm/approach,
  the data structure, the complexity, the topic/category, or what to compute. A
  student who reads ONLY the title must gain no advantage.
- Prefer the scenario, characters, or theme over describing the task.
- Preserve the document's language. Max 255 characters.
- Spoiler (BAD): "Xác định tọa độ cây bị thiếu", "Dijkstra shortest path"
- Neutral (GOOD): "Khu vườn của bác Ba", "Qua cầu trong đêm"
"""

QUIZ_IMPORT_USER_PROMPT = (
    "Analyze the attached document and extract all questions "
    "in the specified JSON format."
)


def normalize_quiz_question_payload(qtype: str, choices, correct_answers):
    """Normalize AI-imported question payloads before preview or persistence."""
    qtype = (qtype or "").upper()

    if isinstance(choices, list):
        normalized_choices = []
        for choice in choices:
            if isinstance(choice, dict) and "id" in choice:
                choice = {**choice, "id": str(choice["id"]).strip().upper()}
            normalized_choices.append(choice)
        choices = normalized_choices
    else:
        choices = None

    if qtype in {"SA", "ES"}:
        choices = []

    if qtype == "ES":
        return choices, None

    if not isinstance(correct_answers, dict) or "answers" not in correct_answers:
        return choices, None

    correct_answers = dict(correct_answers)
    answers = correct_answers.get("answers")
    if answers is None:
        return choices, None

    if qtype in {"MC", "TF"}:
        if not isinstance(answers, str):
            return choices, None
        answer = answers.strip().upper()
        if not answer:
            return choices, None
        correct_answers["answers"] = answer
    elif qtype == "MA":
        if isinstance(answers, str):
            answers = [answers]
        if not isinstance(answers, list):
            return choices, None
        normalized_answers = [
            answer.strip().upper()
            for answer in answers
            if isinstance(answer, str) and answer.strip()
        ]
        if not normalized_answers:
            return choices, None
        correct_answers["answers"] = normalized_answers
    elif qtype == "SA":
        if isinstance(answers, str):
            answers = [answers]
        if not isinstance(answers, list):
            return choices, None
        normalized_answers = [
            str(answer).strip()
            for answer in answers
            if answer is not None and str(answer).strip()
        ]
        if not normalized_answers:
            return choices, None
        answer_type = correct_answers.get("type", "exact")
        if answer_type not in {"exact", "regex"}:
            answer_type = "exact"
        case_sensitive = correct_answers.get("case_sensitive", False)
        correct_answers["type"] = answer_type
        correct_answers["case_sensitive"] = (
            case_sensitive if isinstance(case_sensitive, bool) else False
        )
        correct_answers["answers"] = normalized_answers
    else:
        return choices, None

    return choices, correct_answers


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

        choices, correct_answers = normalize_quiz_question_payload(
            qtype, q.get("choices"), q.get("correct_answers")
        )

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
