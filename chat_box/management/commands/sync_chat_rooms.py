from django.core.management.base import BaseCommand
from django.db import transaction

from judge.models import Organization

from chat_box.models import Room
from chat_box.services.lobby_sync import sync_lobby_memberships
from chat_box.services.organization_sync import (
    audit_organization_channel,
    sync_organization_channel,
)

MAX_SYNC_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Audit or repair Lobby and organization-channel synchronization."

    def add_arguments(self, parser):
        parser.add_argument("--lobby", action="store_true")
        parser.add_argument("--organizations", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=MAX_SYNC_BATCH_SIZE)

    def handle(self, *args, **options):
        include_lobby = options["lobby"]
        include_organizations = options["organizations"]
        if not include_lobby and not include_organizations:
            include_lobby = include_organizations = True
        dry_run = options["dry_run"]
        batch_size = max(1, min(options["batch_size"], MAX_SYNC_BATCH_SIZE))
        self.stdout.write(
            "Chat-room synchronization (%s, SQL batch size %s)"
            % ("dry run" if dry_run else "repair", batch_size)
        )

        if include_lobby:
            report = sync_lobby_memberships(
                dry_run=dry_run,
                batch_size=batch_size,
            )
            self.stdout.write(
                "  Lobby: checked=%(checked)s missing=%(created)s stale=%(updated)s"
                % report
            )

        if include_organizations:
            totals = {
                "channels": 0,
                "missing": 0,
                "extra": 0,
                "wrong_roles": 0,
                "name_drift": 0,
            }
            last_room_id = 0
            while True:
                rows = list(
                    Room.objects.filter(
                        id__gt=last_room_id,
                        channel_kind=Room.ChannelKind.ORGANIZATION,
                        organization_id__isnull=False,
                    )
                    .order_by("id")
                    .values_list("id", "organization_id")[:batch_size]
                )
                if not rows:
                    break
                last_room_id = rows[-1][0]
                organizations = {
                    organization.id: organization
                    for organization in Organization.get_cached_instances(
                        *[organization_id for _, organization_id in rows]
                    )
                }
                for room_id, organization_id in rows:
                    organization = organizations.get(organization_id)
                    if organization is None:
                        continue
                    report = audit_organization_channel(organization)
                    totals["channels"] += 1
                    for key in ("missing", "extra", "wrong_roles"):
                        totals[key] += report[key]
                    totals["name_drift"] += int(report["name_drift"])
                    if not dry_run and any(
                        report[key]
                        for key in ("missing", "extra", "wrong_roles", "name_drift")
                    ):
                        with transaction.atomic():
                            sync_organization_channel(organization)
                            if report["name_drift"]:
                                Room.objects.filter(id=room_id).update(
                                    name=organization.name,
                                    organization_name_snapshot=organization.name,
                                )
                                Room.dirty_cache(room_id)
            self.stdout.write(
                "  Organizations: channels=%(channels)s missing=%(missing)s "
                "extra=%(extra)s stale_roles=%(wrong_roles)s name_drift=%(name_drift)s"
                % totals
            )

        self.stdout.write(self.style.SUCCESS("Chat-room synchronization complete."))
