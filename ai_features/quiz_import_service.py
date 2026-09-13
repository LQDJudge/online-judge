"""
Quiz Import Service — AI-powered question extraction from uploaded files.
Uses Gemini 3 Flash via Poe API to analyze PDF/image/Word documents
and extract structured quiz questions in JSON format.
"""

import json
import logging
import re
from typing import Any, Dict

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

VALID_QUESTION_TYPES = {"MC", "MA", "TF", "SA", "ES"}

QUIZ_IMPORT_SYSTEM_PROMPT = """You are an expert at extracting quiz questions from educational documents.
Your task is to analyze the attached document and extract ALL questions as structured JSON.

QUESTION TYPE DETECTION:
- MC (Multiple Choice): Single correct answer from choices
- MA (Multiple Answer): Multiple correct answers from choices
- TF (True/False): One or more statements, each answered True or False
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
      "correct_answers": {"answers": "B"},
      "suggested_answers": null,
      "suggestion_explanation": ""
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

- TF (one or more true/false statements):
  choices = [{"id": "A", "text": "First statement"}, ...];
  {"answers": {"A": true, "B": false, "C": true, "D": false}}.
  - "answers" maps EVERY statement id to a JSON boolean.
  - Use TF for both a single statement and a group of statements. For one statement,
    use choices = [{"id": "A", "text": "The statement"}] and {"answers": {"A": true}}.
  - Do not create True and False as separate choices. Do not encode this format as MA.
  - Put shared instructions/context in content (or "" if none) and each statement in choices.

- SA (short answer): {"type": "exact", "case_sensitive": false, "answers": ["<answer>", ...]}
  - CRITICAL: the "answers" list is a set of ALTERNATIVE, EQUIVALENT answers. The student
    types ONE answer and is graded correct if it matches ANY ONE entry (logical OR).
  - The list is NOT the parts of a single answer. If the correct answer has multiple parts
    (e.g. the ages of 4 people), it is ONE entry containing the WHOLE answer:
        RIGHT: {"answers": ["Chloe: 5, Leo: 8, Emma: 13, Lily: 15"]}
        WRONG: {"answers": ["Chloe: 5", "Leo: 8", "Emma: 13", "Lily: 15"]}
  - Add more than one entry ONLY when the document explicitly gives equivalent forms
    (e.g. an answer key that says "5 or five" -> ["5", "five"]).
  - REQUIRED ANSWER FORMAT: every SA question's "content" MUST tell the student the exact
    format to type, using PLACEHOLDERS for the values (e.g. <tên>, <số>, <x>, <y>). If you
    include a concrete example of the format, it MUST use invented values that are clearly
    NOT the real answer — NEVER echo the actual answer, which would spoil the question.
        GOOD: "Nhập đáp án đúng định dạng: <Tên>: <số>, ... (ví dụ định dạng: An: 1, Bình: 2)"
        BAD:  "... (ví dụ: Chloe: 5, Leo: 8, Emma: 13, Lily: 15)"  <- this IS the real answer
    If the source already states a format ("Hướng dẫn ghi đáp án", or "write in the format:
    ..."), keep it; otherwise ADD one. Then output ONE canonical "answers" entry that
    follows that exact format.
  - SA answers are graded by NORMALIZED EXACT match: whitespace and letter case are ignored,
    but everything else must match exactly — digits, commas, dots, and the ORDER of parts.
    So write the one canonical answer in the stated format; do not rely on capitalization or
    spacing, and never reorder the parts.

- ES (essay): correct_answers = null (manually graded).

- correct_answers is ONLY for an answer key explicitly provided by the document.
  If the document does not supply a complete key, set correct_answers to null.

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
- Never put your own inferred answer in correct_answers or alter a supplied answer key.
- If a complete document answer key exists, set suggested_answers to null.
- Otherwise, for MC, MA, TF, and SA, try solving the question and return a separate
  suggested_answers object using EXACTLY the same per-type format described above.
  Keep correct_answers null. The author must explicitly apply the suggestion.
- MC: suggest one existing choice ID: {"answers": "B"}.
- MA: suggest the complete set of correct choice IDs: {"answers": ["A", "C"]}.
- TF: suggest a JSON boolean for EVERY statement ID, including false values:
  {"answers": {"A": true, "B": false}}. Do not omit false statements.
- SA: suggest one complete canonical text answer matching the requested input format:
  {"type": "exact", "case_sensitive": false, "answers": ["5"]}.
  Equivalent alternatives are allowed, but never split a multipart answer into entries.
- Include suggestion_explanation: a short plain-text justification in the document's
  language (for TF, briefly explain each statement). Do not put this in question content.
- If you cannot confidently suggest a COMPLETE answer, set suggested_answers to null
  and briefly explain the uncertainty in suggestion_explanation. Do not guess or invent
  missing facts. Preserve any partial document key in that explanation for author review.
- ES: ALWAYS set correct_answers and suggested_answers to null and
  suggestion_explanation to "". Never suggest essay answers.

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

    if qtype == "MC":
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
    elif qtype == "TF":
        if not isinstance(answers, dict) or not isinstance(choices, list):
            return choices, None
        choice_ids = {
            choice.get("id")
            for choice in choices
            if isinstance(choice, dict) and choice.get("id")
        }
        normalized_answers = {
            str(answer_id).strip().upper(): value
            for answer_id, value in answers.items()
            if isinstance(value, bool)
        }
        if not choice_ids or set(normalized_answers) != choice_ids:
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
        if not content and qtype != "TF":
            continue

        # Truncate title
        if len(title) > 255:
            title = title[:252] + "..."
        if not title:
            title = (
                content[:80] + ("..." if len(content) > 80 else "")
                if content
                else _("True/False")
            )

        choices, correct_answers = normalize_quiz_question_payload(
            qtype, q.get("choices"), q.get("correct_answers")
        )
        suggested_answers = None
        suggestion_explanation = ""
        if correct_answers is None and qtype != "ES":
            suggested_answers = normalize_quiz_question_payload(
                qtype, choices, q.get("suggested_answers")
            )[1]
            # Suggestions must refer to real choices, never invented IDs.
            if suggested_answers and qtype in {"MC", "MA"}:
                choice_ids = {
                    choice.get("id")
                    for choice in (choices or [])
                    if isinstance(choice, dict)
                }
                answers = suggested_answers["answers"]
                answer_ids = [answers] if qtype == "MC" else answers
                if not set(answer_ids).issubset(choice_ids):
                    suggested_answers = None
            if suggested_answers and qtype == "SA":
                suggested_answers["type"] = "exact"
                suggested_answers["case_sensitive"] = False
            explanation = q.get("suggestion_explanation")
            if isinstance(explanation, str):
                suggestion_explanation = explanation.strip()

        valid_questions.append(
            {
                "title": title,
                "question_type": qtype,
                "content": content,
                "choices": choices,
                "correct_answers": correct_answers,
                "answer_source": "document" if correct_answers else None,
                "suggested_answers": suggested_answers,
                "suggestion_explanation": suggestion_explanation,
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
