from collections import Counter

from django.conf import settings
from django.core import signing
from django.db import connection
from django.db.models import Count, Q

from chat_box.models import Message, Room, RoomMute, UserRoom

ROOM_LIST_PAGE_SIZE = getattr(settings, "CHAT_ROOM_LIST_PAGE_SIZE", 20)
ROOM_LIST_CURSOR_SALT = "chat-room-list-v1"
ROOM_LIST_SECTIONS = ("channels", "conversations")
UNREAD_COUNT_CAP = 100


def get_lobby():
    return Room.objects.get(singleton_key="lobby")


def get_membership(room, profile, *, for_update=False):
    queryset = UserRoom.objects
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.filter(room_id=room.id, user_id=profile.id).first()


def get_room_page(
    profile,
    *,
    cursor=None,
    archived=False,
    hidden=False,
    limit=None,
    exclude_room_ids=(),
    section=None,
    search="",
):
    limit = min(limit or ROOM_LIST_PAGE_SIZE, 50)
    queryset = UserRoom.objects.filter(user=profile).select_related("room")
    if exclude_room_ids:
        queryset = queryset.exclude(room_id__in=exclude_room_ids)
    if section == "channels":
        queryset = queryset.filter(room__room_type=Room.Type.CHANNEL)
    elif section == "conversations":
        queryset = queryset.filter(
            room__room_type__in=(Room.Type.DIRECT, Room.Type.GROUP)
        )
    search = search.strip()
    if search:
        queryset = queryset.filter(
            Q(room__name__icontains=search)
            | Q(
                room__room_type=Room.Type.DIRECT,
                room__direct_user_low_id=profile.id,
                room__direct_user_high__user__username__icontains=search,
            )
            | Q(
                room__room_type=Room.Type.DIRECT,
                room__direct_user_high_id=profile.id,
                room__direct_user_low__user__username__icontains=search,
            )
        )
    if archived:
        queryset = queryset.filter(
            state=UserRoom.State.ACTIVE,
            room__archived_at__isnull=False,
        )
    else:
        queryset = queryset.filter(
            state=UserRoom.State.ACTIVE,
            room__archived_at__isnull=True,
            is_hidden=hidden,
        )
        if not hidden:
            queryset = queryset.exclude(room__singleton_key="lobby")
    queryset = queryset.order_by("-room__last_activity_at", "-room_id")
    if cursor:
        activity, room_id = cursor
        if activity is None:
            queryset = queryset.filter(
                room__last_activity_at__isnull=True, room_id__lt=room_id
            )
        else:
            queryset = queryset.filter(
                Q(room__last_activity_at__lt=activity)
                | Q(room__last_activity_at=activity, room_id__lt=room_id)
                | Q(room__last_activity_at__isnull=True)
            )
    rows = list(queryset[: limit + 1])
    return rows[:limit], len(rows) > limit


def encode_room_list_cursor(memberships, has_more):
    if not has_more or not memberships:
        return None
    last_room = memberships[-1].room
    return signing.dumps(
        [
            (
                last_room.last_activity_at.isoformat()
                if last_room.last_activity_at
                else None
            ),
            last_room.id,
        ],
        salt=ROOM_LIST_CURSOR_SALT,
        compress=True,
    )


def unread_counts_for_memberships(memberships):
    memberships = list(memberships)
    cursors = {
        membership.room_id: membership.last_read_message_id or 0
        for membership in memberships
        if not membership.is_hidden
    }
    if not cursors:
        return {}
    profile_ids = {membership.user_id for membership in memberships}
    profile_id = next(iter(profile_ids)) if len(profile_ids) == 1 else None
    if connection.features.supports_slicing_ordering_in_compound:
        capped_queries = []
        for room_id, last_read_id in cursors.items():
            query = Message.objects.filter(
                room_id=room_id,
                id__gt=last_read_id,
                kind=Message.Kind.USER,
                hidden=False,
            )
            if profile_id is not None:
                query = query.exclude(author_id=profile_id)
            capped_queries.append(
                query.order_by().values_list("room_id", "id")[:UNREAD_COUNT_CAP]
            )
        rows = capped_queries[0].union(*capped_queries[1:], all=True)
        return dict(Counter(room_id for room_id, _ in rows))

    # SQLite cannot put sliced SELECTs inside a compound query. It is only used
    # by lightweight development/test configurations; retain one query there
    # and cap the displayed result.
    predicate = Q()
    for room_id, last_read_id in cursors.items():
        predicate |= Q(room_id=room_id, id__gt=last_read_id)
    rows = (
        Message.objects.filter(predicate, kind=Message.Kind.USER, hidden=False)
        .values("room_id")
        .annotate(count=Count("id"))
    )
    if profile_id is not None:
        rows = rows.exclude(author_id=profile_id)
    return {row["room_id"]: min(row["count"], UNREAD_COUNT_CAP) for row in rows}


def active_room_mute(room, profile, now):
    return (
        RoomMute.objects.filter(
            room=room,
            target=profile,
            revoked_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
