import secrets

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Count, Q

from judge.caching import cache_wrapper

from chat_box.models import Message, Room, RoomMute, UserRoom

ROOM_LIST_PAGE_SIZE = getattr(settings, "CHAT_ROOM_LIST_PAGE_SIZE", 20)
ROOM_LIST_CURSOR_SALT = "chat-room-list-v1"
ROOM_LIST_SECTIONS = ("channels", "conversations")
UNREAD_COUNT_CAP = 100
UNREAD_CACHE_TIMEOUT = 300


@cache_wrapper(
    prefix="chat_unread_generation_v1",
    expected_type=str,
)
def unread_cache_generation(room_id):
    return secrets.token_urlsafe(12)


def dirty_unread_cache_generation(room_id):
    unread_cache_generation.dirty(room_id)
    transaction.on_commit(lambda: unread_cache_generation.dirty(room_id))


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


def _batch_unread_counts(args_list):
    counts = {}
    args_by_profile = {}
    for room_id, profile_id, last_read_message_id, _generation in args_list:
        args_by_profile.setdefault(profile_id, []).append(
            (room_id, last_read_message_id)
        )

    for profile_id, room_cursors in args_by_profile.items():
        predicate = Q()
        for room_id, last_read_message_id in room_cursors:
            predicate |= Q(room_id=room_id, id__gt=last_read_message_id)
        rows = (
            Message.objects.filter(
                predicate,
                kind=Message.Kind.USER,
                hidden=False,
            )
            .exclude(author_id=profile_id)
            .values("room_id")
            .annotate(count=Count("id"))
        )
        counts.update(
            {
                (profile_id, row["room_id"]): min(
                    row["count"],
                    UNREAD_COUNT_CAP,
                )
                for row in rows
            }
        )

    return [
        counts.get((profile_id, room_id), 0)
        for room_id, profile_id, _last_read_message_id, _generation in args_list
    ]


@cache_wrapper(
    prefix="chat_unread_count_v1",
    timeout=UNREAD_CACHE_TIMEOUT,
    expected_type=int,
    batch_fn=_batch_unread_counts,
)
def _cached_unread_count(
    room_id,
    profile_id,
    last_read_message_id,
    generation,
):
    return _batch_unread_counts(
        [(room_id, profile_id, last_read_message_id, generation)]
    )[0]


def unread_counts_for_memberships(memberships):
    memberships = [membership for membership in memberships if not membership.is_hidden]
    if not memberships:
        return {}

    generations = unread_cache_generation.batch(
        [(membership.room_id,) for membership in memberships]
    )
    args_list = [
        (
            membership.room_id,
            membership.user_id,
            membership.last_read_message_id or 0,
            generation,
        )
        for membership, generation in zip(memberships, generations)
    ]
    unread_counts = _cached_unread_count.batch(args_list)
    return {
        membership.room_id: unread_count
        for membership, unread_count in zip(memberships, unread_counts)
        if unread_count
    }


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
