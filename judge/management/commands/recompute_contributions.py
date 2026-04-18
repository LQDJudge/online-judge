from django.core.management.base import BaseCommand

from judge.models import Profile
from judge.models.profile import get_contribution_rank
from judge.utils.contribution import (
    bulk_compute_contributions,
    compute_contribution,
    detect_abusive_downvoters,
    purge_downvotes_from,
)


class Command(BaseCommand):
    help = (
        "Recompute contribution_points for all profiles. "
        "In bulk mode, first detects and purges downvotes from abusive voters."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            help="Specific username to recompute (default: all users)",
        )
        parser.add_argument(
            "--min-downvotes",
            type=int,
            default=20,
            help="Volume gate for abusive-downvoter detection (default: 20)",
        )
        parser.add_argument(
            "--max-up-ratio",
            type=float,
            default=0.10,
            help="Max ratio of upvotes to downvotes for flagging (default: 0.10)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print detection report and exit without deleting or recomputing",
        )
        parser.add_argument(
            "--skip-purge",
            action="store_true",
            help="Skip downvoter detection and purge (just recompute)",
        )

    def handle(self, *args, **options):
        username = options.get("user")

        if username:
            # Single-user mode: no detection, no purge.
            profiles = Profile.objects.filter(user__username=username)
            if not profiles.exists():
                self.stderr.write(f"User '{username}' not found")
                return
            profile = profiles.first()
            new_points = compute_contribution(profile)
            new_points = max(-2147483648, min(2147483647, new_points))
            Profile.objects.filter(id=profile.id).update(
                contribution_points=new_points,
            )
            get_contribution_rank.dirty(Profile(id=profile.id))
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated {username}: contribution_points = {new_points}"
                )
            )
            return

        min_down = options["min_downvotes"]
        max_ratio = options["max_up_ratio"]
        dry_run = options["dry_run"]
        skip_purge = options["skip_purge"]

        if not skip_purge:
            self.stdout.write(
                f"Detecting abusive downvoters "
                f"(min_downvotes={min_down}, max_up_ratio={max_ratio})..."
            )
            flagged = detect_abusive_downvoters(
                min_downvotes=min_down,
                max_up_ratio=max_ratio,
            )
            self.stdout.write(f"  Flagged {len(flagged)} users.")
            if flagged:
                usernames = list(
                    Profile.objects.filter(id__in=flagged).values_list(
                        "user__username", flat=True
                    )[:20]
                )
                self.stdout.write("  Sample: " + ", ".join(usernames))

            if dry_run:
                self.stdout.write(
                    self.style.WARNING("Dry run: no deletions performed.")
                )
                return

            if flagged:
                self.stdout.write("Purging downvotes...")
                stats = purge_downvotes_from(flagged)
                self.stdout.write(
                    f"  Deleted {stats['pagevote_deleted']} PageVoteVoter rows, "
                    f"{stats['commentvote_deleted']} CommentVote rows."
                )
                self.stdout.write(
                    f"  Rescored {stats['pagevotes_rescored']} PageVotes, "
                    f"{stats['comments_rescored']} Comments."
                )
        elif dry_run:
            self.stdout.write(
                self.style.WARNING("Dry run with --skip-purge: nothing to preview.")
            )
            return

        self.stdout.write("Computing contributions in bulk...")
        scores = bulk_compute_contributions()
        self.stdout.write(f"  Found {len(scores)} profiles with contributions")

        updated = Profile.objects.exclude(contribution_points=0).update(
            contribution_points=0
        )
        self.stdout.write(f"  Reset {updated} profiles to 0")

        batch_size = 500
        profile_ids = list(scores.keys())
        updated = 0
        for i in range(0, len(profile_ids), batch_size):
            batch = profile_ids[i : i + batch_size]
            for pid in batch:
                Profile.objects.filter(id=pid).update(
                    contribution_points=scores[pid],
                )
                get_contribution_rank.dirty(Profile(id=pid))
                updated += 1
            self.stdout.write(
                f"  Updated {min(i + batch_size, len(profile_ids))}"
                f"/{len(profile_ids)} profiles..."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Recomputation complete. {updated} profiles with contributions."
            )
        )
