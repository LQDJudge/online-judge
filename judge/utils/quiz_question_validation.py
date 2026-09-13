"""Shared author/admin validation for the unified quiz question format."""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from judge.models.quiz import QuizQuestionType
from judge.utils.quiz_grading import validate_multiple_true_false_score_table


def configure_question_content_field(form):
    question_type = (
        form.data.get("question_type") if form.is_bound else form.instance.question_type
    )
    form.fields["content"].required = question_type != QuizQuestionType.TRUE_FALSE


def validate_question_data(instance, cleaned_data):
    correct_config = cleaned_data.get("correct_answers")
    is_tf = cleaned_data.get("question_type") == QuizQuestionType.TRUE_FALSE
    if (
        instance.pk
        and (instance.question_type == QuizQuestionType.TRUE_FALSE) != is_tf
        and instance.answers.exists()
    ):
        raise ValidationError(
            _("Cannot change the answer format of a question with existing attempts."),
            code="tf_answer_format_change",
        )

    if cleaned_data.get("question_type") == QuizQuestionType.TRUE_FALSE:
        choices = cleaned_data.get("choices")
        score_table = cleaned_data.get("multiple_true_false_score_table")
        if not isinstance(choices, list) or not choices:
            raise ValidationError(
                _("True/False questions require at least one statement.")
            )

        statement_ids = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValidationError(_("Each statement must be valid."))
            statement_id = choice.get("id")
            statement_text = choice.get("text")
            if (
                not isinstance(statement_id, str)
                or not statement_id.strip()
                or not isinstance(statement_text, str)
                or not statement_text.strip()
            ):
                raise ValidationError(
                    _("Every statement requires an ID and statement text.")
                )
            statement_ids.append(statement_id)

        if len(statement_ids) != len(set(statement_ids)):
            raise ValidationError(_("Statement IDs must be unique."))

        # Saved responses refer to these IDs, not to statement position/text.
        # Adding or reordering statements keeps existing responses addressable.
        if instance.pk and instance.question_type == QuizQuestionType.TRUE_FALSE:
            original_ids = {
                choice["id"]
                for choice in (instance.choices or [])
                if isinstance(choice, dict) and isinstance(choice.get("id"), str)
            }
            if not original_ids.issubset(statement_ids) and instance.answers.exists():
                raise ValidationError(
                    _(
                        "Cannot rename statement IDs or remove statements after answers "
                        "have been saved. Create a new question instead."
                    ),
                    code="tf_statement_ids_change",
                )

        answers = (
            correct_config.get("answers") if isinstance(correct_config, dict) else None
        )
        if not isinstance(answers, dict) or any(
            statement_id not in answers or not isinstance(answers[statement_id], bool)
            for statement_id in statement_ids
        ):
            raise ValidationError(
                _("Select the correct True or False answer for every statement.")
            )
        if set(answers) != set(statement_ids):
            raise ValidationError(_("The answer key contains an unknown statement ID."))

        validate_multiple_true_false_score_table(score_table, len(statement_ids))
