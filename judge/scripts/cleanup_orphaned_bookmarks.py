# Clean up orphaned bookmarks for deleted problems, posts, contests, and solutions
# Run: python3 manage.py shell < judge/scripts/cleanup_orphaned_bookmarks.py

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction

from judge.models import BlogPost, Contest, Problem
from judge.models.bookmark import BookMark, _get_or_create_bookmark
from judge.models.problem import Solution


def cleanup_orphaned_bookmarks_for_model(model, name, dry_run=False):
    """Clean up orphaned bookmarks for a specific model type"""
    content_type = ContentType.objects.get_for_model(model)

    # Get all bookmark object_ids for this content type
    bookmark_object_ids = set(
        BookMark.objects.filter(content_type=content_type).values_list(
            "object_id", flat=True
        )
    )

    if not bookmark_object_ids:
        print(f"No bookmarks found for {name}")
        return 0

    # Get all existing object IDs
    existing_object_ids = set(
        model.objects.filter(id__in=bookmark_object_ids).values_list("id", flat=True)
    )

    # Find orphaned object IDs (bookmarks pointing to deleted objects)
    orphaned_object_ids = bookmark_object_ids - existing_object_ids

    if not orphaned_object_ids:
        print(f"No orphaned bookmarks found for {name}")
        return 0

    print(f"Found {len(orphaned_object_ids)} orphaned bookmarks for {name}")

    if dry_run:
        examples = list(orphaned_object_ids)[:5]
        print(f"  Example orphaned object IDs: {examples}")
        return len(orphaned_object_ids)

    # Get bookmarks to delete
    bookmarks_to_delete = BookMark.objects.filter(
        content_type=content_type, object_id__in=orphaned_object_ids
    )

    # Invalidate caches for affected users
    for bookmark in bookmarks_to_delete:
        profile_ids = list(bookmark.users.values_list("id", flat=True))

        # Invalidate bookmark lookup cache
        _get_or_create_bookmark.dirty(content_type, bookmark.object_id)

        # Invalidate user bookmark caches
        for profile_id in profile_ids:
            cache_key = f"gaboi:{profile_id}"
            cache.delete(cache_key)

    # Delete the bookmarks
    deleted_count = bookmarks_to_delete.delete()[0]
    print(f"Successfully deleted {deleted_count} orphaned bookmarks for {name}")

    return deleted_count


@transaction.atomic
def cleanup_all_orphaned_bookmarks(dry_run=False):
    """Clean up all orphaned bookmarks"""
    print("Starting orphaned bookmark cleanup...")

    if dry_run:
        print("DRY RUN MODE - No changes will be made")

    total_deleted = 0

    models = [
        (Problem, "problems"),
        (BlogPost, "blog posts"),
        (Contest, "contests"),
        (Solution, "solutions"),
    ]

    for model, name in models:
        deleted = cleanup_orphaned_bookmarks_for_model(model, name, dry_run)
        total_deleted += deleted

    print(f"Orphaned bookmark cleanup completed! Total deleted: {total_deleted}")
    return total_deleted


# Run with dry_run=True first to see what would be deleted
# cleanup_all_orphaned_bookmarks(dry_run=True)

# Run for real
cleanup_all_orphaned_bookmarks(dry_run=False)
