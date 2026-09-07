from chat_box.models import Room, UserRoom

ROLE_RANK = {
    None: 0,
    UserRoom.Role.MEMBER: 1,
    UserRoom.Role.MODERATOR: 2,
    UserRoom.Role.ADMIN: 3,
}


def highest_role(*roles):
    return max(roles, key=lambda role: ROLE_RANK.get(role, 0), default=None)


class RoomPolicy:
    """Query-free authorization over already-loaded room and membership facts."""

    def __init__(self, user, profile, room, membership=None):
        self.user = user
        self.profile = profile
        self.room = room
        self.membership = membership

    @property
    def is_active_member(self):
        return bool(self.membership and self.membership.state == UserRoom.State.ACTIVE)

    @property
    def role(self):
        return self.membership.role if self.is_active_member else None

    @property
    def is_admin(self):
        return self.role == UserRoom.Role.ADMIN

    @property
    def is_moderator(self):
        return self.role == UserRoom.Role.MODERATOR

    @property
    def is_superuser_override(self):
        return bool(
            self.user.is_superuser
            and self.is_active_member
            and self.room.room_type != Room.Type.DIRECT
        )

    def can_view(self):
        return self.is_active_member

    def can_post(self):
        return self.is_active_member and self.room.archived_at is None

    def can_react(self):
        return self.can_post()

    def can_hide_room(self):
        return self.is_active_member

    def can_manage(self):
        return self.is_admin or self.is_superuser_override

    def can_rename(self):
        return (
            self.room.archived_at is None
            and self.can_manage()
            and (
                self.room.room_type == Room.Type.GROUP
                or self.room.channel_kind == Room.ChannelKind.CUSTOM
            )
        )

    def can_invite(self):
        return (
            self.room.archived_at is None
            and self.can_manage()
            and (
                self.room.room_type == Room.Type.GROUP
                or self.room.channel_kind == Room.ChannelKind.CUSTOM
            )
        )

    def can_change_avatar(self):
        return (
            self.room.archived_at is None
            and self.can_manage()
            and (
                self.room.room_type == Room.Type.GROUP
                or self.room.channel_kind
                in (Room.ChannelKind.ORGANIZATION, Room.ChannelKind.CUSTOM)
            )
        )

    def can_direct_add(self):
        return (
            self.is_superuser_override
            and self.room.channel_kind != Room.ChannelKind.LOBBY
        )

    def can_view_moderation(self):
        return self.is_admin or self.is_superuser_override

    def can_archive(self):
        return (
            self.room.archived_at is None
            and self.can_manage()
            and (
                self.room.room_type == Room.Type.GROUP
                or self.room.channel_kind == Room.ChannelKind.CUSTOM
            )
        )

    def can_restore(self):
        return (
            self.room.archived_at is not None
            and self.can_manage()
            and (
                self.room.room_type == Room.Type.GROUP
                or self.room.channel_kind == Room.ChannelKind.CUSTOM
            )
        )

    def can_leave(self):
        return (
            self.is_active_member and self.room.channel_kind != Room.ChannelKind.LOBBY
        )

    def can_change_role(self, target_membership):
        return (
            self.can_manage()
            and target_membership.state == UserRoom.State.ACTIVE
            and self.room.room_type != Room.Type.DIRECT
            and self.room.channel_kind != Room.ChannelKind.LOBBY
        )

    def can_remove_or_ban(self, target_membership):
        return (
            self.can_change_role(target_membership)
            and target_membership.user_id != self.profile.id
        )

    def can_moderate_target(self, target_membership):
        if not target_membership or target_membership.state != UserRoom.State.ACTIVE:
            return False
        if target_membership.user_id == self.profile.id:
            return False
        if self.room.room_type == Room.Type.DIRECT:
            return False
        if self.is_superuser_override:
            return True
        if self.is_admin:
            return target_membership.role != UserRoom.Role.ADMIN
        if self.is_moderator:
            return target_membership.role == UserRoom.Role.MEMBER
        return False

    def can_hide_message(self, message, target_membership=None):
        if message.author_id == self.profile.id:
            return self.can_view()
        return self.can_moderate_target(target_membership)
