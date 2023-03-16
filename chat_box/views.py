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
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.urls import reverse

import datetime

from judge import event_poster as event
from judge.jinja2.gravatar import gravatar
from judge.models import Friend

from chat_box.models import Message, Profile, Room, UserRoom, Ignore
from chat_box.utils import encrypt_url, decrypt_url

import json


class ChatView(ListView):
    context_object_name = "message"
    template_name = "chat/chat.html"
    title = _("LQDOJ Chat")

    def __init__(self):
        super().__init__()
        self.room_id = None
        self.room = None
        self.messages = None
        self.page_size = 20

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
        try:
            last_id = int(request.GET.get("last_id"))
        except Exception:
            last_id = 1e15
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
        self.messages = Message.objects.filter(
            hidden=False, room=self.room_id, id__lt=last_id
        )[: self.page_size]
        if not only_messages:
            update_last_seen(request, **kwargs)
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
        context["status_sections"] = get_status_context(self.request)
        context["room"] = self.room_id
        context["has_next"] = self.has_next()
        context["unread_count_lobby"] = get_unread_count(None, self.request.profile)
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


@login_required
def post_message(request):
    ret = {"msg": "posted"}
    if request.method != "POST":
        return HttpResponseBadRequest()
    if len(request.POST["body"]) > 5000:
        return HttpResponseBadRequest()

    room = None
    if request.POST["room"]:
        room = Room.objects.get(id=request.POST["room"])

    if not can_access_room(request, room) or request.profile.mute:
        return HttpResponseBadRequest()

    new_message = Message(author=request.profile, body=request.POST["body"], room=room)
    new_message.save()

    if not room:
        event.post(
            "chat_lobby",
            {
                "type": "lobby",
                "author_id": request.profile.id,
                "message": new_message.id,
                "room": "None",
                "tmp_id": request.POST.get("tmp_id"),
            },
        )
    else:
        for user in room.users():
            event.post(
                "chat_" + str(user.id),
                {
                    "type": "private",
                    "author_id": request.profile.id,
                    "message": new_message.id,
                    "room": room.id,
                    "tmp_id": request.POST.get("tmp_id"),
                },
            )

    return JsonResponse(ret)


def can_access_room(request, room):
    return (
        not room or room.user_one == request.profile or room.user_two == request.profile
    )


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
        if room and not room.contain(request.profile):
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
            room = Room.objects.get(id=int(room_id))
    except Room.DoesNotExist:
        return HttpResponseBadRequest()
    except Exception as e:
        return HttpResponseBadRequest()

    if room and not room.contain(profile):
        return HttpResponseBadRequest()

    user_room, _ = UserRoom.objects.get_or_create(user=profile, room=room)
    user_room.last_seen = timezone.now()
    user_room.save()

    return JsonResponse({"msg": "updated"})


def get_online_count():
    last_two_minutes = timezone.now() - timezone.timedelta(minutes=2)
    return Profile.objects.filter(last_access__gte=last_two_minutes).count()


def get_user_online_status(user):
    time_diff = timezone.now() - user.last_access
    is_online = time_diff <= timezone.timedelta(minutes=2)
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


def get_online_status(request_user, queryset, rooms=None):
    if not queryset:
        return None
    last_two_minutes = timezone.now() - timezone.timedelta(minutes=2)
    ret = []

    if rooms:
        unread_count = get_unread_count(rooms, request_user)
        count = {}
        for i in unread_count:
            count[i["other_user"]] = i["unread_count"]

    for user in queryset:
        is_online = False
        if user.last_access >= last_two_minutes:
            is_online = True
        user_dict = {"user": user, "is_online": is_online}
        if rooms and user.id in count:
            user_dict["unread_count"] = count[user.id]
        user_dict["url"] = encrypt_url(request_user.id, user.id)
        ret.append(user_dict)
    return ret


def get_status_context(request, include_ignored=False):
    if include_ignored:
        ignored_users = Profile.objects.none()
        queryset = Profile.objects
    else:
        ignored_users = Ignore.get_ignored_users(request.profile)
        queryset = Profile.objects.exclude(id__in=ignored_users)

    last_two_minutes = timezone.now() - timezone.timedelta(minutes=2)
    recent_profile = (
        Room.objects.filter(Q(user_one=request.profile) | Q(user_two=request.profile))
        .annotate(
            last_msg_time=Subquery(
                Message.objects.filter(room=OuterRef("pk")).values("time")[:1]
            ),
            other_user=Case(
                When(user_one=request.profile, then="user_two"),
                default="user_one",
            ),
        )
        .filter(last_msg_time__isnull=False)
        .exclude(other_user__in=ignored_users)
        .order_by("-last_msg_time")
        .values("other_user", "id")[:20]
    )

    recent_profile_id = [str(i["other_user"]) for i in recent_profile]
    joined_id = ",".join(recent_profile_id)
    recent_rooms = [int(i["id"]) for i in recent_profile]
    recent_list = None
    if joined_id:
        recent_list = Profile.objects.raw(
            f"SELECT * from judge_profile where id in ({joined_id}) order by field(id,{joined_id})"
        )
    friend_list = (
        Friend.get_friend_profiles(request.profile)
        .exclude(id__in=recent_profile_id)
        .exclude(id__in=ignored_users)
        .order_by("-last_access")
    )
    admin_list = (
        queryset.filter(display_rank="admin")
        .exclude(id__in=friend_list)
        .exclude(id__in=recent_profile_id)
    )
    all_user_status = (
        queryset.filter(display_rank="user", last_access__gte=last_two_minutes)
        .annotate(is_online=Case(default=True, output_field=BooleanField()))
        .order_by("-rating")
        .exclude(id__in=friend_list)
        .exclude(id__in=admin_list)
        .exclude(id__in=recent_profile_id)[:30]
    )

    return [
        {
            "title": "Recent",
            "user_list": get_online_status(request.profile, recent_list, recent_rooms),
        },
        {
            "title": "Following",
            "user_list": get_online_status(request.profile, friend_list),
        },
        {
            "title": "Admin",
            "user_list": get_online_status(request.profile, admin_list),
        },
        {
            "title": "Other",
            "user_list": get_online_status(request.profile, all_user_status),
        },
    ]


@login_required
def online_status_ajax(request):
    return render(
        request,
        "chat/online_status.html",
        {
            "status_sections": get_status_context(request),
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

    if request.method == "GET":
        return JsonResponse({"room": room.id, "other_user_id": other_user.id})
    return HttpResponseRedirect(reverse("chat", kwargs={"room_id": room.id}))


def get_unread_count(rooms, user):
    if rooms:
        mess = (
            Message.objects.filter(
                room=OuterRef("room"), time__gte=OuterRef("last_seen")
            )
            .exclude(author=user)
            .order_by()
            .values("room")
            .annotate(unread_count=Count("pk"))
            .values("unread_count")
        )

        return (
            UserRoom.objects.filter(user=user, room__in=rooms)
            .annotate(
                unread_count=Coalesce(Subquery(mess, output_field=IntegerField()), 0),
                other_user=Case(
                    When(room__user_one=user, then="room__user_two"),
                    default="room__user_one",
                ),
            )
            .filter(unread_count__gte=1)
            .values("other_user", "unread_count")
        )
    else:  # lobby
        mess = (
            Message.objects.filter(room__isnull=True, time__gte=OuterRef("last_seen"))
            .exclude(author=user)
            .order_by()
            .values("room")
            .annotate(unread_count=Count("pk"))
            .values("unread_count")
        )

        res = (
            UserRoom.objects.filter(user=user, room__isnull=True)
            .annotate(
                unread_count=Coalesce(Subquery(mess, output_field=IntegerField()), 0),
            )
            .values_list("unread_count", flat=True)
        )

        return res[0] if len(res) else 0


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
