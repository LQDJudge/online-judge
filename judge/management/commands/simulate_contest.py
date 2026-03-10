import os
import random
import time
import zipfile

import yaml
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    Problem,
    ProblemGroup,
    ProblemType,
    Profile,
    Submission,
    SubmissionSource,
)
from judge.models.problem_data import ProblemData, ProblemTestCase

NUM_PROBLEMS = 5
NUM_USERS = 10
PROBLEM_PREFIX = "sim"
CONTEST_KEY = "simcontest"
USER_PREFIX = "sim_user_"

CORRECT_SOURCE = """\
#include <iostream>
using namespace std;
int main() {
    long long a, b;
    cin >> a >> b;
    cout << a + b << endl;
    return 0;
}
"""

WRONG_SOURCE = """\
#include <iostream>
using namespace std;
int main() {
    long long a, b;
    cin >> a >> b;
    cout << a + b + 1 << endl;
    return 0;
}
"""

PARTIAL_SOURCE = """\
#include <iostream>
using namespace std;
int main() {
    long long a, b;
    cin >> a >> b;
    if (a < 500000)
        cout << a + b << endl;
    else
        cout << a + b + 1 << endl;
    return 0;
}
"""


class Command(BaseCommand):
    help = "Set up, run, or clean up a simulated contest for testing live ranking"

    def add_arguments(self, parser):
        parser.add_argument(
            "--action",
            choices=["setup", "run", "reset", "cleanup"],
            default="run",
            help="Action to perform (default: run)",
        )
        parser.add_argument(
            "--duration",
            type=int,
            default=180,
            help="Simulation duration in seconds (default: 180)",
        )
        parser.add_argument(
            "--users",
            type=int,
            default=NUM_USERS,
            help=f"Number of participants (default: {NUM_USERS})",
        )

    def handle(self, *args, **options):
        action = options["action"]
        num_users = options["users"]
        if action == "setup":
            self._setup(num_users)
        elif action == "run":
            self._run(options["duration"])
        elif action == "reset":
            self._reset()
        elif action == "cleanup":
            self._cleanup(num_users)

    def _setup(self, num_users=NUM_USERS):
        self.stdout.write("Setting up simulation...")

        data_root = settings.DMOJ_PROBLEM_DATA_ROOT
        if not data_root:
            self.stderr.write("DMOJ_PROBLEM_DATA_ROOT is not set")
            return

        group = ProblemGroup.objects.first()
        ptype = ProblemType.objects.first()
        if not group or not ptype:
            self.stderr.write("Need at least one ProblemGroup and ProblemType in DB")
            return

        # Create problems
        problem_codes = []
        for i in range(NUM_PROBLEMS):
            code = f"{PROBLEM_PREFIX}{chr(ord('a') + i)}"
            problem_codes.append(code)

            problem, created = Problem.objects.get_or_create(
                code=code,
                defaults={
                    "name": f"Simulation A+B #{i + 1}",
                    "description": "Read two integers and print their sum.",
                    "time_limit": 2.0,
                    "memory_limit": 262144,
                    "points": 100,
                    "partial": True,
                    "group": group,
                    "is_public": True,
                    "is_manually_managed": False,
                },
            )
            if created:
                problem.types.add(ptype)
                problem.save()
                self.stdout.write(f"  Created problem: {code}")
            else:
                self.stdout.write(f"  Problem already exists: {code}")

            # Create test data on disk with zip archive
            prob_dir = os.path.join(data_root, code)
            os.makedirs(prob_dir, exist_ok=True)

            zip_name = f"{code}.zip"
            zip_path = os.path.join(prob_dir, zip_name)
            test_cases_yaml = []

            with zipfile.ZipFile(zip_path, "w") as zf:
                for t in range(1, 6):
                    a = random.randint(1, 1000000)
                    b = random.randint(1, 1000000)
                    in_file = f"{t}.in"
                    out_file = f"{t}.out"

                    zf.writestr(in_file, f"{a} {b}\n")
                    zf.writestr(out_file, f"{a + b}\n")

                    test_cases_yaml.append(
                        {"in": in_file, "out": out_file, "points": 20}
                    )

            init_yml = {"archive": zip_name, "test_cases": test_cases_yaml}
            with open(os.path.join(prob_dir, "init.yml"), "w") as f:
                yaml.dump(init_yml, f)

            # Create ProblemData with zip reference
            pd, _ = ProblemData.objects.get_or_create(
                problem=problem,
                defaults={"checker": "standard"},
            )
            pd.zipfile.name = f"{code}/{zip_name}"
            pd.save()

            # Create ProblemTestCase records
            if not ProblemTestCase.objects.filter(dataset=problem).exists():
                for t in range(1, 6):
                    ProblemTestCase.objects.create(
                        dataset=problem,
                        order=t,
                        type="C",
                        input_file=f"{t}.in",
                        output_file=f"{t}.out",
                        points=20,
                        is_pretest=False,
                    )

        # Create contest
        now = timezone.now()
        contest, created = Contest.objects.get_or_create(
            key=CONTEST_KEY,
            defaults={
                "name": "Simulation Contest",
                "description": "Auto-generated contest for testing live ranking.",
                "start_time": now,
                "end_time": now + timezone.timedelta(hours=2),
                "is_visible": True,
                "scoreboard_visibility": Contest.SCOREBOARD_VISIBLE,
                "format_name": "default",
            },
        )
        if created:
            self.stdout.write(f"  Created contest: {CONTEST_KEY}")
        else:
            # Update times if contest already exists
            contest.start_time = now
            contest.end_time = now + timezone.timedelta(hours=2)
            contest.save()
            self.stdout.write(f"  Contest already exists, updated times: {CONTEST_KEY}")

        # Add contest problems
        for i, code in enumerate(problem_codes):
            problem = Problem.objects.get(code=code)
            ContestProblem.objects.get_or_create(
                contest=contest,
                problem=problem,
                defaults={
                    "points": 100,
                    "partial": True,
                    "order": i + 1,
                },
            )

        # Create users
        language = Language.objects.get(key="CPP17")
        for i in range(1, num_users + 1):
            username = f"{USER_PREFIX}{i:02d}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@sim.test", "is_active": True},
            )
            if created:
                user.set_password("simpass123")
                user.save()

            profile, _ = Profile.objects.get_or_create(
                user=user,
                defaults={"language": language},
            )

            # Create contest participation
            ContestParticipation.objects.get_or_create(
                contest=contest,
                user=profile,
                virtual=ContestParticipation.LIVE,
                defaults={"real_start": now},
            )
            if created:
                self.stdout.write(f"  Created user: {username}")

        self.stdout.write(self.style.SUCCESS("Setup complete!"))
        self.stdout.write(f"  Contest URL: /contest/{CONTEST_KEY}/ranking")

    def _build_user_plans(self, participations, problems, duration):
        """Give each user an independent submission schedule."""
        import heapq

        plans = []  # min-heap of (time, user_idx, problem_idx, attempt)

        for u_idx, part in enumerate(participations):
            # Each user has a skill level: higher = faster solves, better accuracy
            skill = random.uniform(0.3, 1.0)

            # Each user tackles problems in a shuffled order
            prob_order = list(range(len(problems)))
            random.shuffle(prob_order)

            t = random.uniform(2, 20)  # initial thinking time varies

            for p_idx in prob_order:
                if t >= duration:
                    break
                # Harder problems (later in order) take longer to attempt
                difficulty = 0.6 + 0.4 * (p_idx / max(len(problems) - 1, 1))

                # Number of attempts: 3-4 on average, skilled users sometimes need fewer
                max_attempts = random.randint(2, 5)

                for attempt in range(max_attempts):
                    if t >= duration:
                        break
                    heapq.heappush(
                        plans,
                        (t, u_idx, prob_order[p_idx], attempt, max_attempts, skill),
                    )
                    # Time between retries is shorter than time between new problems
                    t += random.uniform(3, 12) * difficulty / skill

                # Gap before next problem
                t += random.uniform(5, 30) * (1 - skill * 0.5)

        return plans

    def _pick_source(self, attempt, max_attempts, skill):
        """Pick submission source based on attempt number and user skill."""
        # Last attempt has highest chance of being correct
        is_last = attempt == max_attempts - 1
        # Base probability scales with skill
        correct_prob = skill * (0.5 if not is_last else 0.85)
        partial_prob = 0.3 if not is_last else 0.1

        roll = random.random()
        if roll < correct_prob:
            return CORRECT_SOURCE, "correct"
        elif roll < correct_prob + partial_prob:
            return PARTIAL_SOURCE, "partial"
        else:
            return WRONG_SOURCE, "wrong"

    def _run(self, duration):
        self.stdout.write(f"Running simulation for {duration}s...")

        try:
            contest = Contest.objects.get(key=CONTEST_KEY)
        except Contest.DoesNotExist:
            self.stderr.write("Contest not found. Run --action=setup first.")
            return

        language = Language.objects.get(key="CPP17")
        problems = list(
            ContestProblem.objects.filter(contest=contest)
            .select_related("problem")
            .order_by("order")
        )
        participations = list(
            ContestParticipation.objects.filter(
                contest=contest, virtual=ContestParticipation.LIVE
            ).select_related("user", "user__user")
        )

        if not problems or not participations:
            self.stderr.write(
                "No problems or participations found. Run --action=setup first."
            )
            return

        plans = self._build_user_plans(participations, problems, duration)

        import heapq

        start = time.time()
        count = 0

        while plans:
            target_t, u_idx, p_idx, attempt, max_attempts, skill = heapq.heappop(plans)

            # Wait until scheduled time
            now = time.time() - start
            if target_t > now:
                time.sleep(target_t - now)

            elapsed = time.time() - start
            if elapsed > duration:
                break

            participation = participations[u_idx]
            cp = problems[p_idx]
            source, tag = self._pick_source(attempt, max_attempts, skill)
            username = participation.user.user.username

            submission = Submission.objects.create(
                user=participation.user,
                problem=cp.problem,
                language=language,
                contest_object=contest,
                status="QU",
            )
            SubmissionSource.objects.create(
                submission=submission,
                source=source,
            )
            ContestSubmission.objects.create(
                submission=submission,
                problem=cp,
                participation=participation,
            )

            submission.judge()
            count += 1

            self.stdout.write(
                f"  [{int(elapsed // 60):02d}:{int(elapsed % 60):02d}] "
                f"{username} -> {cp.problem.code} ({tag}, "
                f"attempt {attempt + 1}/{max_attempts}) "
                f"[sub #{submission.id}]"
            )

        self.stdout.write(
            self.style.SUCCESS(f"Simulation complete! {count} submissions made.")
        )

    def _reset(self):
        """Delete all submissions and reset participation scores, keep problems/users/contest."""
        self.stdout.write("Resetting scores...")

        try:
            contest = Contest.objects.get(key=CONTEST_KEY)
        except Contest.DoesNotExist:
            self.stderr.write("Contest not found.")
            return

        # Delete all submissions for this contest
        subs = Submission.objects.filter(contest_object=contest)
        count = subs.count()
        subs.delete()
        self.stdout.write(f"  Deleted {count} submissions")

        # Reset participation scores
        participations = ContestParticipation.objects.filter(contest=contest)
        participations.update(score=0, cumtime=0, tiebreaker=0, format_data=None)
        self.stdout.write(f"  Reset {participations.count()} participations")

        # Update contest start time to now
        contest.start_time = timezone.now()
        contest.end_time = timezone.now() + timezone.timedelta(hours=2)
        contest.save()
        self.stdout.write(f"  Contest times reset to now")

        self.stdout.write(self.style.SUCCESS("Reset complete!"))

    def _cleanup(self, num_users=NUM_USERS):
        self.stdout.write("Cleaning up simulation data...")

        data_root = settings.DMOJ_PROBLEM_DATA_ROOT

        # Delete contest (cascades to ContestProblem, ContestParticipation, ContestSubmission)
        deleted, _ = Contest.objects.filter(key=CONTEST_KEY).delete()
        if deleted:
            self.stdout.write(f"  Deleted contest: {CONTEST_KEY}")

        # Delete problems
        for i in range(NUM_PROBLEMS):
            code = f"{PROBLEM_PREFIX}{chr(ord('a') + i)}"
            deleted, _ = Problem.objects.filter(code=code).delete()
            if deleted:
                self.stdout.write(f"  Deleted problem: {code}")

            # Remove test data from disk
            if data_root:
                prob_dir = os.path.join(data_root, code)
                if os.path.isdir(prob_dir):
                    import shutil

                    shutil.rmtree(prob_dir)
                    self.stdout.write(f"  Removed data dir: {prob_dir}")

        # Delete users
        for i in range(1, num_users + 1):
            username = f"{USER_PREFIX}{i:02d}"
            deleted, _ = User.objects.filter(username=username).delete()
            if deleted:
                self.stdout.write(f"  Deleted user: {username}")

        # Clean up orphaned submissions
        Submission.objects.filter(
            problem__isnull=True, user__user__username__startswith=USER_PREFIX
        ).delete()

        self.stdout.write(self.style.SUCCESS("Cleanup complete!"))
