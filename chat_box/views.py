from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.shortcuts import render
from django.db.models import F
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.urls import reverse


from judge import event_poster as event
from judge.caching import cache_wrapper

from chat_box.models import (
    Message,
    Profile,
    Room,
    UserRoom,
    Ignore,
    get_ignored_user_ids,
    get_user_room_list,
    get_first_msg_id,
)
from chat_box.utils import encrypt_url, decrypt_url, encrypt_channel, get_unread_boxes

from reversion import revisions


class ChatView(ListView):
    context_object_name = "message"
    template_name = "chat/chat.html"
    title = _("LQDOJ Chat")

    def __init__(self):
        super().__init__()
        self.room_id = None
        self.room = None
        self.messages = None
        self.first_page_size = 20  # only for first request
        self.follow_up_page_size = 50

    def get_queryset(self):
        return self.messages

    def has_next(self):
        msg_id = get_first_msg_id(self.room_id)
        if not msg_id:
            return False
        return Message(id=msg_id) not in self.messages

    def get(self, request, *args, **kwargs):
        request_room = kwargs["room_id"]
        page_size = self.follow_up_page_size
        try:
            last_id = int(request.GET.get("last_id"))
        except Exception:
            last_id = 1e15
            page_size = self.first_page_size
        only_messages = request.GET.get("only_messages")

        if request_room:
            try:
                self.room = Room.objects.get(id=request_room)
                if not can_access_room(request, self.room):
                    return HttpResponseBadRequest()
            except Room.DoesNotExist:
                return HttpResponseBadRequest()
        else:
            request_room = None

        self.room_id = request_room
        self.messages = list(
            Message.objects.filter(hidden=False, room=self.room_id, id__lt=last_id)[
                :page_size
            ]
        )
        if not only_messages:
            return super().get(request, *args, **kwargs)

        return render(
            request,
            "chat/message_list.html",
            {
                "object_list": self.messages,
                "has_next": self.has_next(),
            },
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context["title"] = self.title
        context["last_msg"] = event.last()
        context["status_sections"] = get_status_context(self.request.profile)
        context["room"] = self.room_id
        context["has_next"] = self.has_next()
        context["unread_count_lobby"] = get_unread_count(None, self.request.profile)
        context["chat_channel"] = encrypt_channel(
            "chat_" + str(self.request.profile.id)
        )
        context["chat_lobby_channel"] = encrypt_channel("chat_lobby")
        if self.room:
            users_room = self.room.get_users()
            other_users = [u for u in users_room if u.id != self.request.profile.id]
            if other_users:
                context["other_user"] = other_users[0]
                context["other_online"] = get_user_online_status(context["other_user"])
                context["is_ignored"] = Ignore.is_ignored(
                    self.request.profile, context["other_user"]
                )
        else:
            context["online_count"] = get_online_count()
        context["message_template"] = {
            "author_id": self.request.profile.id,
            "id": "$id",
            "time": timezone.now(),
            "body": "$body",
        }
        return context


def delete_message(request):
    ret = {"delete": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.get(id=messid)
    except:
        return HttpResponseBadRequest()

    if not request.user.is_staff and request.profile != mess.author:
        return HttpResponseBadRequest()

    room_id = mess.room_id
    mess.hidden = True
    mess.save()

    get_first_msg_id.dirty(room_id)

    # If deleting the last message, update room's last_msg_id
    if room_id:
        room = Room.objects.get(id=room_id)
        if room.last_msg_id == messid:
            # Find the new last visible message
            new_last_msg = (
                Message.objects.filter(room_id=room_id, hidden=False)
                .order_by("-id")
                .first()
            )
            room.last_msg_id = new_last_msg.id if new_last_msg else None
            room.save(update_fields=["last_msg_id"])

        # Dirty the room cache to update last_message in sidebar
        Room.dirty_cache(room_id)

        # Decrement unread_count for users who haven't seen this message yet
        user_rooms = UserRoom.objects.filter(
            room_id=room_id, last_seen__lt=mess.time, unread_count__gt=0
        ).exclude(user=mess.author)
        for user_room in user_rooms:
            user_room.unread_count = max(0, user_room.unread_count - 1)
            user_room.save(update_fields=["unread_count"])
            get_unread_boxes.dirty(user_room.user)

    return JsonResponse(ret)


def mute_message(request):
    ret = {"mute": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    if not request.user.is_staff:
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.get(id=messid)
    except:
        return HttpResponseBadRequest()

    with revisions.create_revision():
        revisions.set_comment(_("Mute chat") + ": " + mess.body)
        revisions.set_user(request.user)
        mess.author.mute = True
        mess.author.save()

    Message.objects.filter(room=None, author=mess.author).update(hidden=True)
    get_first_msg_id.dirty(None)

    return JsonResponse(ret)


def check_valid_message(request, room):
    if not room and len(request.POST["body"]) > 200:
        return False

    if not can_access_room(request, room) or request.profile.mute:
        return False

    last_msg = Message.objects.filter(room=room).first()
    if (
        last_msg
        and last_msg.author == request.profile
        and last_msg.body == request.POST["body"].strip()
    ):
        return False

    if not room:
        four_last_msg = Message.objects.filter(room=room).order_by("-id")[:4]
        if len(four_last_msg) >= 4:
            same_author = all(msg.author == request.profile for msg in four_last_msg)
            time_diff = timezone.now() - four_last_msg[3].time
            if same_author and time_diff.total_seconds() < 300:
                return False

    return True


@login_required
def post_message(request):
    ret = {"msg": "posted"}

    if request.method != "POST":
        return HttpResponseBadRequest()
    if len(request.POST["body"]) > 5000 or len(request.POST["body"].strip()) == 0:
        return HttpResponseBadRequest()

    room = None
    if request.POST["room"]:
        room = Room.objects.get(id=request.POST["room"])

    if not check_valid_message(request, room):
        return HttpResponseBadRequest()

    new_message = Message(author=request.profile, body=request.POST["body"], room=room)
    new_message.save()

    if not room:
        event.post(
            encrypt_channel("chat_lobby"),
            {
                "type": "lobby",
                "author_id": request.profile.id,
                "message": new_message.id,
                "room": "None",
                "tmp_id": request.POST.get("tmp_id"),
            },
        )
        if not get_first_msg_id(None):
            get_first_msg_id.dirty(None)
    else:
        Room.dirty_cache(room.id)
        room.last_msg_id = new_message.id
        room.save()

        # Dirty the user room list cache for all users in the room
        for user in room.get_users():
            get_user_room_list.dirty(user.id)

            event_data = {
                "type": "private",
                "author_id": request.profile.id,
                "message": new_message.id,
                "room": room.id,
                "tmp_id": request.POST.get("tmp_id"),
            }

            if user.id != request.profile.id:
                # Update unread count first, then include in event
                UserRoom.objects.filter(user=user, room=room).update(
                    unread_count=F("unread_count") + 1
                )
                get_unread_boxes.dirty(user)
                # Get the new unread count for this room
                user_room = UserRoom.objects.filter(user=user, room=room).first()
                if user_room:
                    event_data["unread_count"] = user_room.unread_count
                    # Include other user's ID for badge update
                    event_data["other_user_id"] = request.profile.id

            event.post(encrypt_channel("chat_" + str(user.id)), event_data)

        if not get_first_msg_id(room.id):
            get_first_msg_id.dirty(room.id)

    return JsonResponse(ret)


def can_access_room(request, room):
    return not room or room.contain(request.profile)


@login_required
def chat_message_ajax(request):
    if request.method != "GET":
        return HttpResponseBadRequest()

    try:
        message_id = request.GET["message"]
    except KeyError:
        return HttpResponseBadRequest()

    try:
        message = Message.objects.filter(hidden=False).get(id=message_id)
        room = message.room
        if not can_access_room(request, room):
            return HttpResponse("Unauthorized", status=401)
    except Message.DoesNotExist:
        return HttpResponseBadRequest()
    return render(
        request,
        "chat/message.html",
        {
            "message": message,
        },
    )


@login_required
def update_last_seen(request, **kwargs):
    if "room_id" in kwargs:
        room_id = kwargs["room_id"]
    elif request.method == "GET":
        room_id = request.GET.get("room")
    elif request.method == "POST":
        room_id = request.POST.get("room")
    else:
        return HttpResponseBadRequest()
    try:
        profile = request.profile
        room = None
        if room_id:
            room = Room.objects.filter(id=int(room_id)).first()
    except Room.DoesNotExist:
        return HttpResponseBadRequest()

    if not can_access_room(request, room):
        return HttpResponseBadRequest()

    user_room, _ = UserRoom.objects.get_or_create(user=profile, room=room)
    user_room.last_seen = timezone.now()
    user_room.unread_count = 0
    user_room.save()

    get_unread_boxes.dirty(profile)

    return JsonResponse({"msg": "updated"})


@cache_wrapper(prefix="cgoc", timeout=120)
def get_online_count():
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    return Profile.objects.filter(last_access__gte=last_5_minutes).count()


def get_user_online_status(profile):
    time_diff = timezone.now() - profile.get_last_access()
    is_online = time_diff <= timezone.timedelta(minutes=5)
    return is_online


def user_online_status_ajax(request):
    if request.method != "GET":
        return HttpResponseBadRequest()

    user_id = request.GET.get("user")

    if user_id:
        try:
            user_id = int(user_id)
            user = Profile.objects.get(id=user_id)
        except Exception:
            return HttpResponseBadRequest()

        is_online = get_user_online_status(user)
        return render(
            request,
            "chat/user_online_status.html",
            {
                "other_user": user,
                "other_online": is_online,
                "is_ignored": Ignore.is_ignored(request.profile, user),
            },
        )
    else:
        return render(
            request,
            "chat/user_online_status.html",
            {
                "online_count": get_online_count(),
            },
        )


def get_online_status(profile, other_profile_ids, rooms=None):
    if not other_profile_ids:
        return None
    other_profiles = Profile.get_cached_instances(*other_profile_ids)
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    ret = []
    if rooms:
        unread_count = get_unread_count(rooms, profile)
        count = {}
        last_msg = {}
        room_of_user = {}

        # Prefetch room info for all rooms
        Room.prefetch_room_cache(rooms)

        for i in unread_count:
            room_id = i["room"]
            room = Room(id=room_id)
            # Find the other user in a two-person room
            user_ids = room.get_user_ids()
            if len(user_ids) == 2:
                other_id = user_ids[0] if user_ids[1] == profile.id else user_ids[1]
                count[other_id] = i["unread_count"]

        for room_id in rooms:
            room = Room(id=room_id)
            user_ids = room.get_user_ids()
            if len(user_ids) == 2:
                other_id = user_ids[0] if user_ids[1] == profile.id else user_ids[1]
                last_msg[other_id] = room.get_last_message()
                room_of_user[other_id] = room_id

    for other_profile in other_profiles:
        is_online = False
        if other_profile.get_last_access() >= last_5_minutes:
            is_online = True
        user_dict = {"user": other_profile, "is_online": is_online}
        if rooms:
            user_dict.update(
                {
                    "unread_count": count.get(other_profile.id),
                    "last_msg": last_msg.get(other_profile.id),
                    "room": room_of_user.get(other_profile.id),
                }
            )
        user_dict["url"] = encrypt_url(profile.id, other_profile.id)
        ret.append(user_dict)
    return ret


def get_status_context(profile, include_ignored=False):
    if include_ignored:
        ignored_users = []
    else:
        ignored_users = get_ignored_user_ids(profile)

    # Get user's room list sorted by last_msg_time
    user_rooms = get_user_room_list(profile.id)[:20]

    # Prefetch room info for all rooms
    Room.prefetch_room_cache(user_rooms)

    # Get other users from rooms
    recent_profile_ids = []
    recent_rooms = []

    for room_id in user_rooms:
        other_user_id = Room(id=room_id).other_user_id(profile)
        if other_user_id and other_user_id not in ignored_users:
            recent_profile_ids.append(other_user_id)
            recent_rooms.append(room_id)

    admin_ids = [
        i
        for i in get_admin_ids()
        if i not in ignored_users and i not in recent_profile_ids
    ]

    Profile.prefetch_cache_last_access(*(recent_profile_ids + admin_ids))

    return [
        {
            "title": _("Recent"),
            "user_list": get_online_status(profile, recent_profile_ids, recent_rooms),
        },
        {
            "title": _("Admin"),
            "user_list": get_online_status(profile, admin_ids),
        },
    ]


@login_required
def online_status_ajax(request):
    return render(
        request,
        "chat/online_status.html",
        {
            "status_sections": get_status_context(request.profile),
            "unread_count_lobby": get_unread_count(None, request.profile),
        },
    )


@login_required
def get_or_create_room(request):
    if request.method == "GET":
        decrypted_other_id = request.GET.get("other")
    elif request.method == "POST":
        decrypted_other_id = request.POST.get("other")
    else:
        return HttpResponseBadRequest()

    request_id, other_id = decrypt_url(decrypted_other_id)
    if not other_id or not request_id or request_id != request.profile.id:
        return HttpResponseBadRequest()

    try:
        other_user = Profile.objects.get(id=int(other_id))
    except Exception:
        return HttpResponseBadRequest()

    user = request.profile

    if not other_user or not user:
        return HttpResponseBadRequest()

    room = Room.get_or_create_room(other_user, user)

    room_url = reverse("chat", kwargs={"room_id": room.id})
    if request.method == "GET":
        return JsonResponse(
            {
                "room": room.id,
                "other_user_id": other_user.id,
                "url": room_url,
            }
        )
    return HttpResponseRedirect(room_url)


def get_unread_count(rooms, user):
    if rooms:
        return UserRoom.objects.filter(
            user=user, room__in=rooms, unread_count__gt=0
        ).values("unread_count", "room")
    else:  # lobby
        user_room = UserRoom.objects.filter(user=user, room__isnull=True).first()
        if not user_room:
            return 0
        last_seen = user_room.last_seen
        max_lobby_count = 100
        res = (
            Message.objects.filter(room__isnull=True, time__gte=last_seen)
            .exclude(author=user, hidden=True)[:max_lobby_count]
            .count()
        )

        return res


@login_required
def toggle_ignore(request, **kwargs):
    user_id = kwargs["user_id"]
    if not user_id:
        return HttpResponseBadRequest()
    try:
        other_user = Profile.objects.get(id=user_id)
    except:
        return HttpResponseBadRequest()

    Ignore.toggle_ignore(request.profile, other_user)
    get_unread_boxes.dirty(request.profile)
    next_url = request.GET.get("next", "/")
    return HttpResponseRedirect(next_url)


@cache_wrapper(prefix="gai", timeout=24 * 60, expected_type=list)
def get_admin_ids():
    return list(
        Profile.objects.filter(display_rank="admin").values_list("id", flat=True)
    )
