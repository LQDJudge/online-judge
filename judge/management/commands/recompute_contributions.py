from django.core.management.base import BaseCommand

from judge.models import Profile
from judge.utils.contribution import compute_contribution, bulk_compute_contributions


class Command(BaseCommand):
    help = "Recompute contribution_points for all profiles"

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            help="Specific username to recompute (default: all users)",
        )

    def handle(self, *args, **options):
        username = options.get("user")

        if username:
            profiles = Profile.objects.filter(user__username=username)
            if not profiles.exists():
                self.stderr.write(f"User '{username}' not found")
                return
            profile = profiles.first()
            new_points = compute_contribution(profile)
            new_points = max(-2147483648, min(2147483647, new_points))
            Profile.objects.filter(id=profile.id).update(contribution_points=new_points)
            Profile.dirty_cache(profile.id)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated {username}: contribution_points = {new_points}"
                )
            )
            return

        self.stdout.write("Computing contributions in bulk...")
        scores = bulk_compute_contributions()
        self.stdout.write(f"  Found {len(scores)} profiles with contributions")

        # Bulk update: set all profiles to 0 first, then update those with scores
        updated = Profile.objects.exclude(contribution_points=0).update(
            contribution_points=0
        )
        self.stdout.write(f"  Reset {updated} profiles to 0")

        # Update profiles with non-zero scores
        batch_size = 500
        profile_ids = list(scores.keys())
        updated = 0
        for i in range(0, len(profile_ids), batch_size):
            batch = profile_ids[i : i + batch_size]
            for pid in batch:
                Profile.objects.filter(id=pid).update(contribution_points=scores[pid])
                updated += 1
            Profile.dirty_cache(*batch)
            self.stdout.write(
                f"  Updated {min(i + batch_size, len(profile_ids))}/{len(profile_ids)} profiles..."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Recomputation complete. {updated} profiles with contributions."
            )
        )
