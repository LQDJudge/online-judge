from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.forms.models import model_to_dict
from django.db.models import (
    Case,
    BooleanField,
    When,
    Q,
    Subquery,
    OuterRef,
    Exists,
    Count,
    IntegerField,
    F,
    Max,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.urls import reverse


from judge import event_poster as event
from judge.jinja2.gravatar import gravatar
from judge.models import Friend

from chat_box.models import Message, Profile, Room, UserRoom, Ignore, get_room_info
from chat_box.utils import encrypt_url, decrypt_url, encrypt_channel, get_unread_boxes


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
        try:
            msg = Message.objects.filter(room=self.room_id).earliest("id")
        except Exception as e:
            return False
        return msg not in self.messages

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
        self.messages = (
            Message.objects.filter(hidden=False, room=self.room_id, id__lt=last_id)
            .select_related("author")
            .only("body", "time", "author__rating", "author__display_rank")[:page_size]
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
            users_room = [self.room.user_one, self.room.user_two]
            users_room.remove(self.request.profile)
            context["other_user"] = users_room[0]
            context["other_online"] = get_user_online_status(context["other_user"])
            context["is_ignored"] = Ignore.is_ignored(
                self.request.profile, context["other_user"]
            )
        else:
            context["online_count"] = get_online_count()
        context["message_template"] = {
            "author": self.request.profile,
            "id": "$id",
            "time": timezone.now(),
            "body": "$body",
        }
        return context


def delete_message(request):
    ret = {"delete": "done"}

    if request.method == "GET":
        return HttpResponseBadRequest()

    if not request.user.is_staff:
        return HttpResponseBadRequest()

    try:
        messid = int(request.POST.get("message"))
        mess = Message.objects.get(id=messid)
    except:
        return HttpResponseBadRequest()

    mess.hidden = True
    mess.save()

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

    mess.author.mute = True
    mess.author.save()
    Message.objects.filter(room=None, author=mess.author).update(hidden=True)

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
    else:
        get_room_info.dirty(room.id)
        room.last_msg_time = new_message.time
        room.save()

        for user in room.users():
            event.post(
                encrypt_channel("chat_" + str(user.id)),
                {
                    "type": "private",
                    "author_id": request.profile.id,
                    "message": new_message.id,
                    "room": room.id,
                    "tmp_id": request.POST.get("tmp_id"),
                },
            )
            if user != request.profile:
                UserRoom.objects.filter(user=user, room=room).update(
                    unread_count=F("unread_count") + 1
                )
                get_unread_boxes.dirty(user)

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


def get_online_count():
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    return Profile.objects.filter(last_access__gte=last_5_minutes).count()


def get_user_online_status(user):
    time_diff = timezone.now() - user.last_access
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
        except Exception as e:
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
    Profile.prefetch_profile_cache(other_profile_ids)

    joined_ids = ",".join([str(id) for id in other_profile_ids])
    other_profiles = Profile.objects.raw(
        f"SELECT * from judge_profile where id in ({joined_ids}) order by field(id,{joined_ids})"
    )
    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    ret = []
    if rooms:
        unread_count = get_unread_count(rooms, profile)
        count = {}
        last_msg = {}
        room_of_user = {}
        for i in unread_count:
            room = Room.objects.get(id=i["room"])
            other_profile = room.other_user(profile)
            count[other_profile.id] = i["unread_count"]
        rooms = Room.objects.filter(id__in=rooms)
        for room in rooms:
            other_profile_id = room.other_user_id(profile)
            last_msg[other_profile_id] = room.last_message_body()
            room_of_user[other_profile_id] = room.id

    for other_profile in other_profiles:
        is_online = False
        if other_profile.last_access >= last_5_minutes:
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
        queryset = Profile.objects
    else:
        ignored_users = list(
            Ignore.get_ignored_users(profile).values_list("id", flat=True)
        )
        queryset = Profile.objects.exclude(id__in=ignored_users)

    last_5_minutes = timezone.now() - timezone.timedelta(minutes=5)
    recent_profile = (
        Room.objects.filter(Q(user_one=profile) | Q(user_two=profile))
        .annotate(
            other_user=Case(
                When(user_one=profile, then="user_two"),
                default="user_one",
            ),
        )
        .filter(last_msg_time__isnull=False)
        .exclude(other_user__in=ignored_users)
        .order_by("-last_msg_time")
        .values("other_user", "id")[:20]
    )

    recent_profile_ids = [str(i["other_user"]) for i in recent_profile]
    recent_rooms = [int(i["id"]) for i in recent_profile]
    Room.prefetch_room_cache(recent_rooms)

    admin_list = (
        queryset.filter(display_rank="admin")
        .exclude(id__in=recent_profile_ids)
        .values_list("id", flat=True)
    )

    return [
        {
            "title": _("Recent"),
            "user_list": get_online_status(profile, recent_profile_ids, recent_rooms),
        },
        {
            "title": _("Admin"),
            "user_list": get_online_status(profile, admin_list),
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
def get_room(user_one, user_two):
    if user_one.id > user_two.id:
        user_one, user_two = user_two, user_one
    room, created = Room.objects.get_or_create(user_one=user_one, user_two=user_two)
    return room


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
    # TODO: each user can only create <= 300 rooms
    room = get_room(other_user, user)
    for u in [other_user, user]:
        user_room, created = UserRoom.objects.get_or_create(user=u, room=room)
        if created:
            user_room.last_seen = timezone.now()
            user_room.save()

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
        res = (
            Message.objects.filter(room__isnull=True, time__gte=last_seen)
            .exclude(author=user)
            .exclude(hidden=True)
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
    next_url = request.GET.get("next", "/")
    return HttpResponseRedirect(next_url)
