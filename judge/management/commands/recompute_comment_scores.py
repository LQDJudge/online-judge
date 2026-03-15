from django.core.management.base import BaseCommand
from django.db.models import Sum

from judge.models.comment import Comment, CommentVote


class Command(BaseCommand):
    help = "Recompute Comment.score from CommentVote records to fix any drift"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show mismatches without updating",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Compute correct score for each comment that has votes
        vote_sums = dict(
            CommentVote.objects.values("comment_id")
            .annotate(total=Sum("score"))
            .values_list("comment_id", "total")
        )

        # Find comments whose score doesn't match
        mismatched = 0
        fixed = 0

        # Comments with votes: check score matches
        comment_ids_with_votes = list(vote_sums.keys())
        batch_size = 1000
        for i in range(0, len(comment_ids_with_votes), batch_size):
            batch = comment_ids_with_votes[i : i + batch_size]
            comments = Comment.objects.filter(id__in=batch).values_list("id", "score")
            for cid, current_score in comments:
                expected = vote_sums[cid]
                if current_score != expected:
                    mismatched += 1
                    if not dry_run:
                        Comment.objects.filter(id=cid).update(score=expected)
                        Comment.dirty_cache(cid)
                        fixed += 1
                    else:
                        self.stdout.write(
                            f"  Comment {cid}: {current_score} -> {expected}"
                        )

        # Comments with non-zero score but no votes: reset to 0
        no_vote_nonzero = Comment.objects.filter(score__gt=0).exclude(
            id__in=comment_ids_with_votes
        )
        no_vote_nonzero_neg = Comment.objects.filter(score__lt=0).exclude(
            id__in=comment_ids_with_votes
        )

        for qs in [no_vote_nonzero, no_vote_nonzero_neg]:
            for comment in qs.values_list("id", "score"):
                cid, current_score = comment
                mismatched += 1
                if not dry_run:
                    Comment.objects.filter(id=cid).update(score=0)
                    Comment.dirty_cache(cid)
                    fixed += 1
                else:
                    self.stdout.write(
                        f"  Comment {cid}: {current_score} -> 0 (no votes)"
                    )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"Dry run: {mismatched} mismatched comments found.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Fixed {fixed} comment scores."))
