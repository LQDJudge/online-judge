# Generated migration for refactoring Room model to use UserRoom

from django.db import migrations, models
import django.db.models.deletion


def migrate_existing_rooms(apps, schema_editor):
    """Migrate existing rooms to create UserRoom entries and convert last_msg_time to last_msg_id"""
    Room = apps.get_model("chat_box", "Room")
    UserRoom = apps.get_model("chat_box", "UserRoom")
    Message = apps.get_model("chat_box", "Message")

    for room in Room.objects.all():
        if room.user_one_id and room.user_two_id:
            # Create UserRoom entries if they don't exist
            UserRoom.objects.get_or_create(
                user_id=room.user_one_id, room=room, defaults={"unread_count": 0}
            )
            UserRoom.objects.get_or_create(
                user_id=room.user_two_id, room=room, defaults={"unread_count": 0}
            )

        # Find the last message for this room to set last_msg_id
        last_msg = (
            Message.objects.filter(room=room, hidden=False).order_by("-id").first()
        )
        if last_msg:
            room.last_msg_id = last_msg.id
            room.save()


def reverse_migrate(apps, schema_editor):
    """Reverse migration - convert last_msg_id back to last_msg_time"""
    Room = apps.get_model("chat_box", "Room")
    Message = apps.get_model("chat_box", "Message")

    for room in Room.objects.all():
        if room.last_msg_id:
            try:
                last_msg = Message.objects.get(id=room.last_msg_id)
                room.last_msg_time = last_msg.time
                room.save()
            except Message.DoesNotExist:
                pass


class Migration(migrations.Migration):

    dependencies = [
        ("chat_box", "0016_alter_room_unique_together"),
    ]

    operations = [
        migrations.AddField(
            model_name="room",
            name="last_msg_id",
            field=models.IntegerField(
                null=True, verbose_name="last message id", db_index=True
            ),
        ),
        migrations.RemoveField(
            model_name="room",
            name="last_msg_time",
        ),
        migrations.RunPython(migrate_existing_rooms, reverse_migrate),
    ]
