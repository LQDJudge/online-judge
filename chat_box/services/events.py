from judge import event_poster as event

from chat_box.models import Ignore, UserRoom
from chat_box.selectors import get_room_page
from chat_box.utils import encrypt_channel


def room_event_channel(room_id):
    return encrypt_channel("chat_room_%s" % room_id)


def personal_event_channel(profile_id):
    return encrypt_channel("chat_%s" % profile_id)


def authorized_event_room_ids(profile, current_room_id, requested_room_ids=None):
    ignored_room_ids = Ignore.get_ignored_room_ids(profile)
    if requested_room_ids is None:
        memberships, _ = get_room_page(
            profile,
            exclude_room_ids=ignored_room_ids,
        )
    else:
        requested_room_ids = sorted(set(requested_room_ids))[:63]
        memberships = list(
            UserRoom.objects.filter(
                user=profile,
                room_id__in=requested_room_ids,
                state=UserRoom.State.ACTIVE,
                is_hidden=False,
            )
            .exclude(room_id__in=ignored_room_ids)
            .select_related("room")
        )
    optional_room_ids = {membership.room_id for membership in memberships}
    lobby_id = (
        UserRoom.objects.filter(
            user=profile,
            state=UserRoom.State.ACTIVE,
            is_hidden=False,
            room__singleton_key="lobby",
        )
        .values_list("room_id", flat=True)
        .first()
    )
    # Always reserve a subscription for the open room, then Lobby when visible.
    # Fill the remaining budget with sidebar rooms before sorting for the grant.
    room_ids = [current_room_id]
    if lobby_id and lobby_id != current_room_id:
        room_ids.append(lobby_id)
    for room_id in sorted(optional_room_ids.difference(room_ids)):
        if len(room_ids) >= 63:
            break
        room_ids.append(room_id)
    return sorted(room_ids)


def chat_event_channels(profile_id, room_ids):
    return [personal_event_channel(profile_id)] + [
        room_event_channel(room_id) for room_id in room_ids
    ]


def broadcast_room_event(room_id, payload):
    event.post(room_event_channel(room_id), payload)


def broadcast_personal_event(profile_id, payload):
    event.post(personal_event_channel(profile_id), payload)


def broadcast_personal_events(profile_ids, payload):
    event.post_many(
        [
            {
                "channel": personal_event_channel(profile_id),
                "message": payload,
            }
            for profile_id in dict.fromkeys(profile_ids)
        ]
    )


def revoke_room_subscriptions(profile_id, room_id):
    event.post(
        "__chat_revoke_user_room__",
        {
            "user_id": profile_id,
            "room_id": room_id,
            "channel": room_event_channel(room_id),
        },
    )


def revoke_room_subscriptions_many(profile_ids, room_id):
    channel = room_event_channel(room_id)
    event.post_many(
        [
            {
                "channel": "__chat_revoke_user_room__",
                "message": {
                    "user_id": profile_id,
                    "room_id": room_id,
                    "channel": channel,
                },
            }
            for profile_id in dict.fromkeys(profile_ids)
        ]
    )
