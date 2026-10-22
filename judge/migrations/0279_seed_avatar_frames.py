from django.db import migrations

FRAMES = (
    ("course-iron", "Course · Sắt", "iron", "COURSE", "cp-sat"),
    ("course-bronze", "Course · Đồng", "bronze", "COURSE", "cp-dong"),
    ("course-silver", "Course · Bạc", "silver", "COURSE", "cp-bac"),
    ("course-gold", "Course · Vàng", "gold", "COURSE", "cp-vang"),
    ("cses", "CSES · Hoàn thành", "cses", "CSES", "cses"),
    ("problem-setter", "Problem Setter", "setter", "SETTER", None),
)


def seed_frames(apps, schema_editor):
    Frame = apps.get_model("judge", "AvatarFrame")
    Course = apps.get_model("judge", "Course")
    alias = schema_editor.connection.alias
    courses = dict(
        Course.objects.using(alias)
        .filter(slug__in=[row[4] for row in FRAMES if row[4]])
        .values_list("slug", "pk")
    )
    for order, (slug, name, style, label, course_slug) in enumerate(FRAMES):
        Frame.objects.using(alias).get_or_create(
            slug=slug,
            defaults={
                "name": name,
                "style": style,
                "label": label,
                "order": order,
                "unlock_type": "course" if course_slug else "manual",
                "course_id": courses.get(course_slug),
                "description": (
                    "Đạt 100% điểm khóa học để mở khóa khung này."
                    if course_slug
                    else "Được quản trị viên trao tặng vì đóng góp ra đề chất lượng."
                ),
            },
        )


class Migration(migrations.Migration):
    dependencies = [("judge", "0278_avatar_frames")]
    operations = [migrations.RunPython(seed_frames, migrations.RunPython.noop)]
