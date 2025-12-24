"""
Quiz Models Test Script for Django Shell

Run this script with:
    python3 manage.py shell < test_quiz_models.py

Or copy-paste sections into:
    python3 manage.py shell
"""

print("=" * 60)
print("QUIZ MODELS TEST SCRIPT")
print("=" * 60)

# Import models
from judge.models import (
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
    QuizAttempt,
    QuizAnswer,
    QuizAnswerFile,
    CourseLessonQuiz,
    Profile,
)
from django.utils import timezone

# Get a test user
user = Profile.objects.first()
if not user:
    print("ERROR: No Profile found. Create a user first.")
    exit()

print(f"\nUsing test user: {user.user.username}")

# ============================================================
# TEST 1: QuizQuestion Creation
# ============================================================
print("\n" + "=" * 60)
print("TEST 1: QuizQuestion Creation")
print("=" * 60)

# Multiple Choice Question
q_mc = QuizQuestion.objects.create(
    question_type="MC",
    title="Math MC Question",
    content="What is 2 + 2?",
    choices=[
        {"id": "a", "text": "3"},
        {"id": "b", "text": "4"},
        {"id": "c", "text": "5"},
        {"id": "d", "text": "6"},
    ],
    correct_answers={"answers": "b"},
    default_points=1.0,
    tags="math, basic, arithmetic",
    explanation="2 + 2 = 4 is a basic arithmetic operation.",
)
print(f"✓ Created MC question: {q_mc}")

# Multiple Answer Question
q_ma = QuizQuestion.objects.create(
    question_type="MA",
    title="Prime Numbers Question",
    content="Select all prime numbers:",
    choices=[
        {"id": "a", "text": "2"},
        {"id": "b", "text": "3"},
        {"id": "c", "text": "4"},
        {"id": "d", "text": "5"},
    ],
    correct_answers={"answers": ["a", "b", "d"]},
    default_points=2.0,
    tags="math, primes",
)
print(f"✓ Created MA question: {q_ma}")

# True/False Question
q_tf = QuizQuestion.objects.create(
    question_type="TF",
    title="Earth Shape Question",
    content="The Earth is flat.",
    choices=[{"id": "true", "text": "True"}, {"id": "false", "text": "False"}],
    correct_answers={"answers": "false"},
    default_points=1.0,
)
print(f"✓ Created TF question: {q_tf}")

# Short Answer Question
q_sa = QuizQuestion.objects.create(
    question_type="SA",
    title="Capital of France",
    content="What is the capital of France?",
    correct_answers={
        "type": "exact",
        "answers": ["Paris", "paris"],
        "case_sensitive": False,
    },
    default_points=1.0,
)
print(f"✓ Created SA question: {q_sa}")

# Essay Question
q_es = QuizQuestion.objects.create(
    question_type="ES",
    title="Essay on Programming",
    content="Write a short essay about why programming is important.",
    default_points=5.0,
)
print(f"✓ Created ES question: {q_es}")

# ============================================================
# TEST 2: QuizQuestion Methods
# ============================================================
print("\n" + "=" * 60)
print("TEST 2: QuizQuestion Methods")
print("=" * 60)

print(f"get_tags_list(): {q_mc.get_tags_list()}")
print(f"has_tag('math'): {q_mc.has_tag('math')}")
print(f"has_tag('science'): {q_mc.has_tag('science')}")
print(f"is_editable_by(user): {q_mc.is_editable_by(user.user)}")

# Test shuffle choices
print(f"get_choices_for_attempt(seed=123): {q_mc.get_choices_for_attempt(seed=123)}")

# ============================================================
# TEST 3: Quiz Creation
# ============================================================
print("\n" + "=" * 60)
print("TEST 3: Quiz Creation")
print("=" * 60)

quiz = Quiz.objects.create(
    code="testquiz001",
    title="Test Quiz - Phase 1 Verification",
    description="This is a test quiz to verify Phase 1 models.",
    time_limit=30,
    shuffle_questions=True,
    is_shown_answer=True,
)
print(f"✓ Created quiz: {quiz}")
print(f"  Code: {quiz.code}")
print(f"  Time limit: {quiz.time_limit} minutes")

# ============================================================
# TEST 4: QuizQuestionAssignment
# ============================================================
print("\n" + "=" * 60)
print("TEST 4: QuizQuestionAssignment")
print("=" * 60)

assignments = [
    QuizQuestionAssignment.objects.create(quiz=quiz, question=q_mc, points=10, order=1),
    QuizQuestionAssignment.objects.create(quiz=quiz, question=q_ma, points=20, order=2),
    QuizQuestionAssignment.objects.create(quiz=quiz, question=q_tf, points=10, order=3),
    QuizQuestionAssignment.objects.create(quiz=quiz, question=q_sa, points=15, order=4),
    QuizQuestionAssignment.objects.create(quiz=quiz, question=q_es, points=45, order=5),
]
print(f"✓ Created {len(assignments)} question assignments")

# ============================================================
# TEST 5: Quiz Methods
# ============================================================
print("\n" + "=" * 60)
print("TEST 5: Quiz Methods")
print("=" * 60)

print(f"get_total_points(): {quiz.get_total_points()}")
print(f"get_questions(): {list(quiz.get_questions())}")
print(f"is_editable_by(user): {quiz.is_editable_by(user.user)}")
print(f"is_accessible_by(user): {quiz.is_accessible_by(user.user)}")
print(f"show_answers(user): {quiz.show_answers(user.user)}")

# ============================================================
# TEST 6: QuizAttempt Creation
# ============================================================
print("\n" + "=" * 60)
print("TEST 6: QuizAttempt Creation")
print("=" * 60)

attempt = QuizAttempt.objects.create(
    user=user, quiz=quiz, time_limit_minutes=quiz.time_limit, attempt_number=1
)
print(f"✓ Created attempt: {attempt}")
print(f"  Start time: {attempt.start_time}")
print(f"  Time limit: {attempt.time_limit_minutes} minutes")

# ============================================================
# TEST 7: QuizAttempt Methods
# ============================================================
print("\n" + "=" * 60)
print("TEST 7: QuizAttempt Methods")
print("=" * 60)

print(f"time_remaining(): {attempt.time_remaining()} seconds")
print(f"is_expired(): {attempt.is_expired()}")
print(f"is_submitted: {attempt.is_submitted}")
print(f"duration: {attempt.duration}")

# ============================================================
# TEST 8: QuizAnswer Creation and Auto-grading
# ============================================================
print("\n" + "=" * 60)
print("TEST 8: QuizAnswer Creation and Auto-grading")
print("=" * 60)

# Answer MC correctly
ans_mc = QuizAnswer.objects.create(attempt=attempt, question=q_mc, answer="b")
graded = ans_mc.auto_grade()
print(
    f"MC Answer 'b' (correct): graded={graded}, is_correct={ans_mc.is_correct}, points={ans_mc.points}"
)

# Answer MA correctly
import json

ans_ma = QuizAnswer.objects.create(
    attempt=attempt, question=q_ma, answer=json.dumps(["a", "b", "d"])
)
graded = ans_ma.auto_grade()
print(
    f"MA Answer ['a','b','d'] (correct): graded={graded}, is_correct={ans_ma.is_correct}, points={ans_ma.points}"
)

# Answer TF incorrectly
ans_tf = QuizAnswer.objects.create(attempt=attempt, question=q_tf, answer="true")
graded = ans_tf.auto_grade()
print(
    f"TF Answer 'true' (wrong): graded={graded}, is_correct={ans_tf.is_correct}, points={ans_tf.points}"
)

# Answer SA correctly
ans_sa = QuizAnswer.objects.create(attempt=attempt, question=q_sa, answer="Paris")
graded = ans_sa.auto_grade()
print(
    f"SA Answer 'Paris' (correct): graded={graded}, is_correct={ans_sa.is_correct}, points={ans_sa.points}"
)

# Answer ES (cannot be auto-graded)
ans_es = QuizAnswer.objects.create(
    attempt=attempt, question=q_es, answer="Programming is important because..."
)
graded = ans_es.auto_grade()
print(
    f"ES Answer (essay): graded={graded}, is_correct={ans_es.is_correct}, points={ans_es.points}"
)

# ============================================================
# TEST 9: Calculate Score
# ============================================================
print("\n" + "=" * 60)
print("TEST 9: Calculate Score")
print("=" * 60)

total, max_score = attempt.calculate_score()
print(f"Total score: {total}")
print(f"Max score: {max_score}")
print(f"Percentage: {(total/max_score)*100:.1f}%" if max_score > 0 else "N/A")

# ============================================================
# TEST 10: Auto-submit
# ============================================================
print("\n" + "=" * 60)
print("TEST 10: Auto-submit")
print("=" * 60)

attempt.auto_submit()
print(f"After auto_submit():")
print(f"  is_submitted: {attempt.is_submitted}")
print(f"  end_time: {attempt.end_time}")
print(f"  duration: {attempt.duration}")
print(f"  score: {attempt.score}/{attempt.max_score}")

# ============================================================
# TEST 11: get_formatted_answer
# ============================================================
print("\n" + "=" * 60)
print("TEST 11: get_formatted_answer")
print("=" * 60)

print(f"MC formatted: {ans_mc.get_formatted_answer()}")
print(f"MA formatted: {ans_ma.get_formatted_answer()}")
print(f"TF formatted: {ans_tf.get_formatted_answer()}")
print(f"SA formatted: {ans_sa.get_formatted_answer()}")

# ============================================================
# CLEANUP
# ============================================================
print("\n" + "=" * 60)
print("CLEANUP")
print("=" * 60)

# Delete quiz (cascades to assignments, attempts, answers)
quiz.delete()
print("✓ Deleted quiz and related objects")

# Delete questions
q_mc.delete()
q_ma.delete()
q_tf.delete()
q_sa.delete()
q_es.delete()
print("✓ Deleted all test questions")

print("\n" + "=" * 60)
print("ALL TESTS COMPLETED SUCCESSFULLY!")
print("=" * 60)
