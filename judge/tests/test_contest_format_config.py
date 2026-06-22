from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from judge.contest_format.atcoder import AtCoderContestFormat
from judge.contest_format.base import MAX_FORMAT_BONUS_POINTS, MAX_PENALTY_MINUTES
from judge.contest_format.ecoo import ECOOContestFormat
from judge.contest_format.icpc import ICPCContestFormat


class ContestFormatConfigValidationTests(SimpleTestCase):
    def test_atcoder_rejects_absurd_penalty(self):
        AtCoderContestFormat.validate({"penalty": MAX_PENALTY_MINUTES})

        with self.assertRaises(ValidationError):
            AtCoderContestFormat.validate({"penalty": MAX_PENALTY_MINUTES + 1})

    def test_icpc_rejects_absurd_penalty(self):
        ICPCContestFormat.validate({"penalty": MAX_PENALTY_MINUTES})

        with self.assertRaises(ValidationError):
            ICPCContestFormat.validate({"penalty": MAX_PENALTY_MINUTES + 1})

    def test_ecoo_rejects_absurd_bonus_config(self):
        ECOOContestFormat.validate(
            {
                "cumtime": True,
                "first_ac_bonus": MAX_FORMAT_BONUS_POINTS,
                "time_bonus": MAX_PENALTY_MINUTES,
            }
        )

        with self.assertRaises(ValidationError):
            ECOOContestFormat.validate(
                {"cumtime": True, "first_ac_bonus": MAX_FORMAT_BONUS_POINTS + 1}
            )

        with self.assertRaises(ValidationError):
            ECOOContestFormat.validate(
                {"cumtime": True, "time_bonus": MAX_PENALTY_MINUTES + 1}
            )
