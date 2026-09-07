import secrets

from django.core.signing import BadSignature, Signer
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int, int_to_base36
from django.utils.translation import gettext as _

from chat_box.exceptions import RoomNotFound, RoomPermissionDenied
from chat_box.models import Room, RoomInvitation, RoomModerationLog, UserRoom
from chat_box.policies import RoomPolicy
from chat_box.services.memberships import activate_membership

INVITATION_SIGNER = Signer(salt="chat-room-invitation-v1")
INVITATION_SIGNATURE_LENGTH = 16


def _new_nonce():
    return secrets.token_urlsafe(32)


def _serialize(invitation):
    value = "%s:%s" % (invitation.room_id, invitation.nonce)
    signature = INVITATION_SIGNER.signature(value)[:INVITATION_SIGNATURE_LENGTH]
    return "%s.%s" % (int_to_base36(invitation.room_id), signature)


def _authorized_locked_room(room, user, profile):
    room = Room.objects.select_for_update().get(pk=room.pk)
    membership = UserRoom.objects.filter(room=room, user=profile).first()
    if not RoomPolicy(user, profile, room, membership).can_invite():
        raise RoomPermissionDenied(_("You cannot access this room's invitation."))
    return room


def _locked_invitation(room, profile):
    return RoomInvitation.objects.select_for_update().get_or_create(
        room=room,
        defaults={"nonce": _new_nonce(), "created_by": profile},
    )[0]


@transaction.atomic
def get_invitation_token(room, user, profile):
    room = _authorized_locked_room(room, user, profile)
    invitation = _locked_invitation(room, profile)
    if invitation.revoked_at is not None:
        return None
    return _serialize(invitation)


@transaction.atomic
def rotate_invitation(room, user, profile):
    room = _authorized_locked_room(room, user, profile)
    invitation = _locked_invitation(room, profile)
    invitation.nonce = _new_nonce()
    invitation.rotated_at = timezone.now()
    invitation.revoked_at = None
    invitation.created_by = profile
    invitation.save()
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.INVITE_ROTATE,
        actor=profile,
    )
    return _serialize(invitation)


@transaction.atomic
def revoke_invitation(room, user, profile):
    room = _authorized_locked_room(room, user, profile)
    invitation = _locked_invitation(room, profile)
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at"])
    RoomModerationLog.objects.create(
        room=room,
        action=RoomModerationLog.Action.INVITE_REVOKE,
        actor=profile,
    )


def resolve_invitation(token):
    try:
        room_code, supplied_signature = token.split(".", 1)
        invitation = RoomInvitation.objects.select_related("room").get(
            room_id=base36_to_int(room_code), revoked_at__isnull=True
        )
        if not constant_time_compare(
            supplied_signature,
            _serialize(invitation).split(".", 1)[1],
        ):
            raise BadSignature
    except (BadSignature, ValueError, RoomInvitation.DoesNotExist):
        # Links generated before short invitation URLs were introduced remain
        # valid until the room administrator rotates or revokes them.
        try:
            raw = INVITATION_SIGNER.unsign(token)
            room_id, nonce = raw.split(":", 1)
            invitation = RoomInvitation.objects.select_related("room").get(
                room_id=int(room_id),
                nonce=nonce,
                revoked_at__isnull=True,
            )
        except (BadSignature, ValueError, RoomInvitation.DoesNotExist):
            raise RoomNotFound(
                _("This invitation is invalid or has been revoked."),
                code="invalid_invitation",
            )
    if invitation.revoked_at is not None:
        raise RoomNotFound(
            _("This invitation is invalid or has been revoked."),
            code="invalid_invitation",
        )
    room = invitation.room
    if room.archived_at is not None or not (
        room.room_type == Room.Type.GROUP
        or room.channel_kind == Room.ChannelKind.CUSTOM
    ):
        raise RoomNotFound(
            _("This invitation is no longer available."), code="invalid_invitation"
        )
    return invitation


@transaction.atomic
def join_from_invitation(token, profile):
    invitation = resolve_invitation(token)
    room = Room.objects.select_for_update().get(pk=invitation.room_id)
    locked_invitation = RoomInvitation.objects.select_for_update().get(room=room)
    if locked_invitation.nonce != invitation.nonce or locked_invitation.revoked_at:
        raise RoomNotFound(
            _("This invitation is invalid or has been revoked."),
            code="invalid_invitation",
        )
    membership, membership_created = activate_membership(
        room, profile, profile, announce=True
    )
    return room, membership
