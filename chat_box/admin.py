from django.contrib import admin

from chat_box.models import (
    Room,
    RoomBan,
    RoomInvitation,
    RoomModerationLog,
    RoomMute,
    RoomRedirect,
    UserRoom,
)


class ReadOnlyChatAdmin(admin.ModelAdmin):
    """Keep inspection in Django admin without bypassing chat domain services."""

    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Room)
class RoomAdmin(ReadOnlyChatAdmin):
    list_display = (
        "id",
        "room_type",
        "channel_kind",
        "name",
        "organization_id",
        "last_activity_at",
        "archived_at",
    )
    list_filter = ("room_type", "channel_kind", "archived_at")
    search_fields = ("=id", "name", "organization_name_snapshot")
    raw_id_fields = (
        "organization",
        "direct_user_low",
        "direct_user_high",
        "archived_by",
    )


@admin.register(RoomRedirect)
class RoomRedirectAdmin(ReadOnlyChatAdmin):
    list_display = ("old_room_id", "canonical_room_id", "created_at")
    search_fields = ("=old_room_id", "=canonical_room__id")
    raw_id_fields = ("canonical_room",)


@admin.register(UserRoom)
class UserRoomAdmin(ReadOnlyChatAdmin):
    list_display = ("id", "room_id", "user_id", "state", "role", "is_hidden")
    list_filter = ("state", "role", "is_hidden")
    search_fields = ("=room__id", "user__user__username")
    raw_id_fields = ("room", "user")


@admin.register(RoomInvitation)
class RoomInvitationAdmin(ReadOnlyChatAdmin):
    list_display = ("room_id", "created_by_id", "created_at", "revoked_at")
    search_fields = ("=room__id", "room__name", "created_by__user__username")
    raw_id_fields = ("room", "created_by")


@admin.register(RoomMute)
class RoomMuteAdmin(ReadOnlyChatAdmin):
    list_display = (
        "id",
        "room_id",
        "target_id",
        "muted_by_id",
        "expires_at",
        "revoked_at",
    )
    list_filter = ("revoked_at",)
    search_fields = ("=room__id", "room__name", "target__user__username")
    raw_id_fields = ("room", "target", "muted_by", "revoked_by")


@admin.register(RoomBan)
class RoomBanAdmin(ReadOnlyChatAdmin):
    list_display = (
        "id",
        "room_id",
        "target_id",
        "banned_by_id",
        "created_at",
        "revoked_at",
    )
    list_filter = ("revoked_at",)
    search_fields = ("=room__id", "room__name", "target__user__username")
    raw_id_fields = ("room", "target", "banned_by", "revoked_by")


@admin.register(RoomModerationLog)
class RoomModerationLogAdmin(ReadOnlyChatAdmin):
    list_display = ("id", "room_id", "action", "actor_id", "target_id", "created_at")
    list_filter = ("action",)
    search_fields = (
        "=room__id",
        "room__name",
        "actor__user__username",
        "target__user__username",
        "reason",
    )
    raw_id_fields = ("room", "actor", "target", "message")
