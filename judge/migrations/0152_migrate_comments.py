from django.db import migrations, models
import django.db.models.deletion
from django.core.exceptions import ObjectDoesNotExist


def migrate_comments(apps, schema_editor):
    Comment = apps.get_model("judge", "Comment")
    Problem = apps.get_model("judge", "Problem")
    Solution = apps.get_model("judge", "Solution")
    BlogPost = apps.get_model("judge", "BlogPost")
    Contest = apps.get_model("judge", "Contest")

    for comment in Comment.objects.all():
        page = comment.page
        try:
            if page.startswith("p:"):
                code = page[2:]
                comment.linked_object = Problem.objects.get(code=code)
            elif page.startswith("s:"):
                code = page[2:]
                comment.linked_object = Solution.objects.get(problem__code=code)
            elif page.startswith("c:"):
                key = page[2:]
                comment.linked_object = Contest.objects.get(key=key)
            elif page.startswith("b:"):
                blog_id = page[2:]
                comment.linked_object = BlogPost.objects.get(id=blog_id)
            comment.save()
        except ObjectDoesNotExist:
            comment.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("judge", "0151_comment_content_type"),
    ]

    operations = [
        migrations.RunPython(migrate_comments, migrations.RunPython.noop, atomic=True),
        migrations.AlterField(
            model_name="comment",
            name="content_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="contenttypes.contenttype",
            ),
        ),
        migrations.AlterField(
            model_name="comment",
            name="object_id",
            field=models.PositiveIntegerField(),
        ),
    ]
