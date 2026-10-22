"""Room-authorized thread endpoints; room and thread read cursors stay independent."""

from django.db import IntegrityError, transaction
from django.db.models import BigIntegerField, F, Max, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from judge.utils.community import can_use_community_features
from chat_box.exceptions import RoomError, RoomNotFound, RoomPermissionDenied
from chat_box.models import Ignore, Message, Room, ThreadParticipation, UserRoom
from chat_box.policies import RoomPolicy
from chat_box.room_views import room_api
from chat_box.selectors import active_room_mute
from chat_box.services.events import broadcast_room_event
from chat_box.thread_data import thread_summaries
from chat_box.views import (
    build_reply_snippet,
    check_valid_message,
    is_chat_muted,
    message_permission_context,
    message_render_context,
    reaction_render_context,
    reply_render_context,
)

THREAD_PAGE_SIZE = 30


def resolve_thread(request, room_id, root_id, *, lock=False):
    rooms = Room.objects.select_for_update() if lock else Room.objects
    room = rooms.get(pk=room_id)
    memberships = UserRoom.objects.filter(room=room, user=request.profile)
    if lock:
        memberships = memberships.select_for_update()
    membership = memberships.first()
    if not RoomPolicy(request.user, request.profile, room, membership).can_view():
        raise RoomPermissionDenied(_("You cannot view this room."))
    roots = Message.objects.filter(
        pk=root_id, room=room, thread_root__isnull=True, kind=Message.Kind.USER
    )
    if lock:
        roots = roots.select_for_update()
    root = roots.first()
    if root is None or room.room_type == Room.Type.DIRECT:
        raise RoomNotFound(_("This thread is unavailable."), code="thread_unavailable")
    return room, membership, root


def render_thread_messages(request, room, membership, root, messages):
    can_post = (
        not root.hidden
        and RoomPolicy(request.user, request.profile, room, membership).can_post()
        and not is_chat_muted(request.profile)
        and not active_room_mute(room, request.profile, timezone.now())
        and can_use_community_features(request.user, request.profile)
        and not (
            request.in_contest and request.participation.contest.use_clarifications
        )
    )
    context = {
        "thread_messages": messages,
        "room_object": room,
        "thread_view": True,
        "message_dom_prefix": "thread-",
        "can_interact_room": can_post,
        **message_render_context(messages),
        **message_permission_context(
            messages, request.user, request.profile, room, membership
        ),
        **reaction_render_context(messages, request.profile),
        **reply_render_context(messages, request.user),
    }
    return (
        render_to_string("chat/thread_messages.html", context, request=request),
        can_post,
    )


def thread_payload(request, room, membership, root, before=None):
    replies = Message.objects.filter(
        thread_root=root, room=room, hidden=False, kind=Message.Kind.USER
    )
    if before is not None:
        replies = replies.filter(id__lt=before)
    page = list(replies.order_by("-id")[: THREAD_PAGE_SIZE + 1])
    has_more = len(page) > THREAD_PAGE_SIZE
    page = list(reversed(page[:THREAD_PAGE_SIZE]))
    root_html, can_post = render_thread_messages(
        request, room, membership, root, [] if root.hidden else [root]
    )
    replies_html, _can_post = render_thread_messages(
        request, room, membership, root, page
    )
    return {
        "room": room.id,
        "root_id": root.id,
        "root_hidden": root.hidden,
        "root_html": root_html,
        "replies_html": replies_html,
        "summary": thread_summaries([root], request.profile)[root.id],
        "can_post": can_post,
        "max_length": 200 if room.channel_kind == Room.ChannelKind.LOBBY else 5000,
        "has_more": has_more,
        "before": page[0].id if page else None,
        "reply_ids": [message.id for message in page],
    }


@room_api
@require_GET
def thread_detail(request, room_id, root_id):
    room, membership, root = resolve_thread(request, room_id, root_id)
    before = request.GET.get("before")
    try:
        before = int(before) if before else None
    except ValueError:
        raise RoomError(_("Invalid thread cursor."))
    return JsonResponse(thread_payload(request, room, membership, root, before))


@room_api
@require_POST
@transaction.atomic
def thread_reply(request, room_id, root_id):
    room, membership, root = resolve_thread(request, room_id, root_id, lock=True)
    if root.hidden:
        raise RoomError(_("The original message was hidden. This thread is read-only."))
    body = request.POST.get("body", "")
    if not body.strip() or len(body) > 5000:
        raise RoomError(_("Messages must contain between 1 and 5,000 characters."))
    nonce = request.POST.get("client_nonce", "")
    if not nonce or len(nonce) > 64:
        raise RoomError(_("Invalid message identifier."))
    previous = Message.objects.filter(
        author=request.profile, client_nonce=nonce
    ).first()
    if previous:
        if (
            previous.thread_root_id != root.id
            or previous.body != body.strip()
            or previous.hidden
        ):
            raise RoomError(_("This message identifier has already been used."))
        return JsonResponse(
            {
                "message": previous.id,
                "summary": thread_summaries([root], request.profile)[root.id],
            }
        )
    check_valid_message(request, room, membership)
    try:
        with transaction.atomic():
            message = Message.objects.create(
                room=room,
                author=request.profile,
                body=body,
                thread_root=root,
                client_nonce=nonce,
            )
    except IntegrityError:
        raise RoomError(_("This message identifier has already been used."), status=409)
    ThreadParticipation.objects.update_or_create(
        root=root, user=request.profile, defaults={"following": True}
    )
    if root.author_id:
        ThreadParticipation.objects.get_or_create(root=root, user_id=root.author_id)
    # Sending in a thread does not mark the channel or other replies as read.
    transaction.on_commit(
        lambda: broadcast_room_event(
            room.id,
            {
                "type": "thread_reply",
                "room": room.id,
                "thread_root_id": root.id,
                "message": message.id,
                "author_id": request.profile.id,
                "tmp_id": nonce,
            },
        )
    )
    return JsonResponse(
        {
            "message": message.id,
            "summary": thread_summaries([root], request.profile)[root.id],
        }
    )


@room_api
@require_POST
@transaction.atomic
def thread_read(request, room_id, root_id):
    room, _membership, root = resolve_thread(request, room_id, root_id, lock=True)
    try:
        cursor = int(request.POST.get("last_reply_id", ""))
    except ValueError:
        raise RoomError(_("Invalid thread cursor."))
    if not Message.objects.filter(
        pk=cursor, thread_root=root, room=room, hidden=False
    ).exists():
        raise RoomError(_("Invalid thread cursor."))
    participation, _created = ThreadParticipation.objects.get_or_create(
        root=root, user=request.profile, defaults={"following": False}
    )
    ThreadParticipation.objects.filter(pk=participation.pk).filter(
        Q(last_read_reply_id__isnull=True) | Q(last_read_reply_id__lt=cursor)
    ).update(last_read_reply_id=cursor)
    return JsonResponse({"summary": thread_summaries([root], request.profile)[root.id]})


@room_api
@require_POST
@transaction.atomic
def thread_follow(request, room_id, root_id):
    _room, _membership, root = resolve_thread(request, room_id, root_id, lock=True)
    value = request.POST.get("following")
    if value not in ("true", "false"):
        raise RoomError(_("Invalid follow preference."))
    ThreadParticipation.objects.update_or_create(
        root=root, user=request.profile, defaults={"following": value == "true"}
    )
    return JsonResponse({"summary": thread_summaries([root], request.profile)[root.id]})


@room_api
@require_GET
def followed_threads(request):
    visible_roots = Message.objects.filter(
        thread_members__user=request.profile,
        thread_members__following=True,
        room__userroom__user=request.profile,
        room__userroom__state=UserRoom.State.ACTIVE,
        room__userroom__is_hidden=False,
        thread_root__isnull=True,
    ).exclude(room_id__in=Ignore.get_ignored_room_ids(request.profile))
    roots = visible_roots.annotate(
        activity=Max("thread_replies__id", filter=Q(thread_replies__hidden=False))
    ).order_by("-activity", "-id")
    try:
        offset = max(0, min(int(request.GET.get("offset", "0")), 100000))
    except ValueError:
        raise RoomError(_("Invalid thread cursor."))
    page = list(roots[offset : offset + THREAD_PAGE_SIZE + 1])
    has_more = len(page) > THREAD_PAGE_SIZE
    page = page[:THREAD_PAGE_SIZE]
    summaries = thread_summaries(page, request.profile)
    rooms = {
        room.id: room
        for room in Room.get_cached_instances(*{root.room_id for root in page})
    }
    cursor = ThreadParticipation.objects.filter(
        root_id=OuterRef("thread_root_id"), user=request.profile
    ).values("last_read_reply_id")[:1]
    unread_count = len(
        list(
            Message.objects.filter(
                thread_root_id__in=visible_roots.values("id"),
                room_id=F("thread_root__room_id"),
                hidden=False,
                kind=Message.Kind.USER,
            )
            .annotate(
                read_cursor=Coalesce(
                    Subquery(cursor), Value(0), output_field=BigIntegerField()
                )
            )
            .filter(id__gt=F("read_cursor"))
            .exclude(author_id=request.profile.id)
            .values_list("id", flat=True)[:100]
        )
    )
    return JsonResponse(
        {
            "unread_count": unread_count,
            "threads": [
                {
                    "id": root.id,
                    "room": root.room_id,
                    "room_name": rooms[root.room_id].name or _("Lobby"),
                    "snippet": (
                        _("Original message unavailable")
                        if root.hidden
                        else build_reply_snippet(root.body)
                    ),
                    "summary": summaries[root.id],
                }
                for root in page
            ],
            "has_more": has_more,
            "next_offset": offset + len(page),
        }
    )
