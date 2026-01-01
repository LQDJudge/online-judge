"""
Django management command for auto-moderating community organizations using LLM
Usage: python manage.py auto_moderate [options]
"""

import sys
import os
import json
from django.core.management.base import BaseCommand
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

# Add llm_service to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", ".."))

from llm_service.llm_api import LLMService
from judge.models import (
    Organization,
    BlogPost,
    Comment,
    OrganizationModerationLog,
)


# Static system prompts for LLM caching
COMMENT_SYSTEM_PROMPT = """You are a content moderator. Review comments and decide if each should be HIDDEN or KEPT.

Respond ONLY with valid JSON array: [{"id": <comment_id>, "action": "hide" or "keep"}]

HIDE only if the comment is clearly harmful:
- Spam or advertising
- Hate speech, slurs, or severe harassment
- Threats or illegal content
- Personal attacks or doxxing

KEEP everything else, including off-topic, criticism, low-effort, or casual comments.
When in doubt, KEEP the comment."""

POST_SYSTEM_PROMPT = """You are a content moderator. Review blog posts and decide if each should be APPROVED, REJECTED, or SKIPPED.

Respond ONLY with valid JSON array: [{"id": <post_id>, "action": "approve" or "reject" or "skip"}]

APPROVE: on-topic, appropriate content.
REJECT only if clearly harmful: spam, hate speech, harassment, threats.
SKIP: uncertain, needs human review.
When in doubt, SKIP for human review."""

# User prompts with variable content
COMMENT_USER_PROMPT = """Community: {about}

Comments to review:
{comments}"""

POST_USER_PROMPT = """Community: {about}

Posts to review:
{posts}"""


class Command(BaseCommand):
    help = "Auto-moderate community organizations using LLM"

    def add_arguments(self, parser):
        parser.add_argument(
            "--org-ids",
            type=str,
            help="Comma-separated organization IDs (default: all communities)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview decisions without taking action",
        )
        parser.add_argument(
            "--comments-only",
            action="store_true",
            help="Only moderate comments",
        )
        parser.add_argument(
            "--posts-only",
            action="store_true",
            help="Only moderate pending blog posts",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Number of items to process per batch (default: 50)",
        )

    def handle(self, *args, **options):
        # Get LLM settings
        api_key = getattr(settings, "POE_API_KEY", None)
        if not api_key:
            self.stderr.write(self.style.ERROR("POE_API_KEY not found in settings"))
            return

        bot_name = getattr(settings, "POE_BOT_NAME", "Claude-3.7-Sonnet")

        try:
            self.llm_service = LLMService(
                api_key=api_key,
                bot_name=bot_name,
            )
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"Failed to initialize LLM service: {e}")
            )
            return

        self.dry_run = options["dry_run"]
        self.batch_size = options["batch_size"]

        # Get organizations
        if options["org_ids"]:
            org_ids = [int(x.strip()) for x in options["org_ids"].split(",")]
            organizations = Organization.objects.filter(id__in=org_ids)
        else:
            # Default to all communities
            organizations = Organization.objects.filter(is_community=True)

        if not organizations.exists():
            self.stdout.write(self.style.WARNING("No organizations found"))
            return

        self.stdout.write(
            self.style.SUCCESS(f"Processing {organizations.count()} organization(s)")
        )
        if self.dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        # Process each organization
        total_stats = {
            "comments_hidden": 0,
            "comments_kept": 0,
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "errors": 0,
        }

        for org in organizations:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(
                self.style.SUCCESS(f"Organization: {org.name} (ID: {org.id})")
            )
            self.stdout.write(f"{'='*60}")

            org_stats = self.process_organization(org, options)
            for key in total_stats:
                total_stats[key] += org_stats.get(key, 0)

        # Print summary
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(self.style.SUCCESS("SUMMARY"))
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"Comments hidden: {total_stats['comments_hidden']}")
        self.stdout.write(f"Comments kept: {total_stats['comments_kept']}")
        self.stdout.write(f"Posts approved: {total_stats['posts_approved']}")
        self.stdout.write(f"Posts rejected: {total_stats['posts_rejected']}")
        self.stdout.write(f"Posts skipped: {total_stats['posts_skipped']}")
        self.stdout.write(f"Errors: {total_stats['errors']}")

    def process_organization(self, org, options):
        stats = {
            "comments_hidden": 0,
            "comments_kept": 0,
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "errors": 0,
        }

        about = org.about or "General community"

        # Process comments
        if not options["posts_only"]:
            comment_stats = self.moderate_comments(org, about)
            for key in comment_stats:
                stats[key] += comment_stats[key]

        # Process pending posts
        if not options["comments_only"]:
            post_stats = self.moderate_posts(org, about)
            for key in post_stats:
                stats[key] += post_stats[key]

        return stats

    def moderate_comments(self, org, about):
        """Moderate comments on organization blog posts (batched)"""
        stats = {"comments_hidden": 0, "comments_kept": 0, "errors": 0}

        # Get visible blog posts in this organization
        blog_posts = BlogPost.objects.filter(
            organizations=org,
            visible=True,
        )

        if not blog_posts.exists():
            self.stdout.write("  No visible blog posts found")
            return stats

        blog_content_type = ContentType.objects.get_for_model(BlogPost)
        comment_content_type = ContentType.objects.get_for_model(Comment)
        blog_post_ids = list(blog_posts.values_list("id", flat=True))

        # Get comment IDs already reviewed (in moderation log)
        already_reviewed = OrganizationModerationLog.objects.filter(
            organization=org,
            content_type=comment_content_type,
        ).values_list("object_id", flat=True)

        # Get unhidden comments on these posts, excluding already reviewed
        comments = list(
            Comment.objects.filter(
                content_type=blog_content_type,
                object_id__in=blog_post_ids,
                hidden=False,
            )
            .exclude(id__in=already_reviewed)
            .select_related("author")[: self.batch_size]
        )

        if not comments:
            self.stdout.write("  No comments to review")
            return stats

        self.stdout.write(f"  Reviewing {len(comments)} comments in one batch...")

        # Build batch prompt
        comments_text = []
        comments_map = {}
        for comment in comments:
            author_name = comment.author.username if comment.author else "Anonymous"
            content = (comment.body or "").strip()
            if not content:
                continue
            comments_map[comment.id] = comment
            comments_text.append(
                f"[Comment ID: {comment.id}] by {author_name}:\n{content[:500]}"
            )

        if not comments_text:
            self.stdout.write("  No non-empty comments to review")
            return stats

        user_prompt = COMMENT_USER_PROMPT.format(
            about=about[:1000],
            comments="\n\n---\n\n".join(comments_text),
        )

        try:
            response = self.llm_service.call_llm(
                user_prompt, system_prompt=COMMENT_SYSTEM_PROMPT
            )
            if not response:
                self.stdout.write(self.style.WARNING("  LLM returned no response"))
                stats["errors"] = len(comments_map)
                return stats

            results = self.parse_json_response(response)
            if not results or not isinstance(results, list):
                self.stdout.write(
                    self.style.WARNING(f"  Failed to parse response: {response[:200]}")
                )
                stats["errors"] = len(comments_map)
                return stats

            # Process results
            for result in results:
                comment_id = result.get("id")
                action = result.get("action", "").lower()

                if comment_id not in comments_map:
                    continue

                comment = comments_map[comment_id]
                author_name = comment.author.username if comment.author else "Anonymous"

                if action == "hide":
                    if not self.dry_run:
                        comment.hidden = True
                        comment.save(update_fields=["hidden"])
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=comment,
                            action="hide_comment",
                            is_automated=True,
                        )
                    stats["comments_hidden"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    HIDDEN: {author_name} - {comment.body[:50]}..."
                        )
                    )
                else:
                    if not self.dry_run:
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=comment,
                            action="keep_comment",
                            is_automated=True,
                        )
                    stats["comments_kept"] += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Batch complete: {stats['comments_kept']} kept, {stats['comments_hidden']} hidden"
                )
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Error: {e}"))
            stats["errors"] = len(comments_map)

        return stats

    def moderate_posts(self, org, about):
        """Moderate pending blog posts in organization (batched)"""
        stats = {
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "errors": 0,
        }

        # Get pending blog posts (visible=False, not rejected)
        pending_posts = list(
            BlogPost.objects.filter(
                organizations=org,
                visible=False,
                is_rejected=False,
            ).prefetch_related("authors")[: self.batch_size]
        )

        if not pending_posts:
            self.stdout.write("  No pending posts to review")
            return stats

        self.stdout.write(f"  Reviewing {len(pending_posts)} posts in one batch...")

        # Build batch prompt
        posts_text = []
        posts_map = {}
        for post in pending_posts:
            authors = post.authors.all()
            author_name = (
                ", ".join(a.username for a in authors) if authors else "Anonymous"
            )
            title = post.title or "Untitled"
            content = (post.content or "").strip()
            if not content:
                continue
            posts_map[post.id] = post
            posts_text.append(
                f"[Post ID: {post.id}] Title: {title}\nAuthor: {author_name}\nContent:\n{content[:1000]}"
            )

        if not posts_text:
            self.stdout.write("  No non-empty posts to review")
            return stats

        user_prompt = POST_USER_PROMPT.format(
            about=about[:1000],
            posts="\n\n---\n\n".join(posts_text),
        )

        try:
            response = self.llm_service.call_llm(
                user_prompt, system_prompt=POST_SYSTEM_PROMPT
            )
            if not response:
                self.stdout.write(self.style.WARNING("  LLM returned no response"))
                stats["errors"] = len(posts_map)
                return stats

            results = self.parse_json_response(response)
            if not results or not isinstance(results, list):
                self.stdout.write(
                    self.style.WARNING(f"  Failed to parse response: {response[:200]}")
                )
                stats["errors"] = len(posts_map)
                return stats

            # Process results
            for result in results:
                post_id = result.get("id")
                action = result.get("action", "").lower()

                if post_id not in posts_map:
                    continue

                post = posts_map[post_id]
                title = post.title or "Untitled"

                if action == "approve":
                    if not self.dry_run:
                        post.visible = True
                        post.save(update_fields=["visible"])
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=post,
                            action="approve_post",
                            is_automated=True,
                        )
                    stats["posts_approved"] += 1
                    self.stdout.write(self.style.SUCCESS(f"    APPROVED: {title[:50]}"))

                elif action == "reject":
                    if not self.dry_run:
                        post.is_rejected = True
                        post.save(update_fields=["is_rejected"])
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=post,
                            action="reject_post",
                            is_automated=True,
                        )
                    stats["posts_rejected"] += 1
                    self.stdout.write(self.style.ERROR(f"    REJECTED: {title[:50]}"))

                else:  # skip or unknown
                    if not self.dry_run:
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=post,
                            action="skip",
                            is_automated=True,
                        )
                    stats["posts_skipped"] += 1
                    self.stdout.write(self.style.WARNING(f"    SKIPPED: {title[:50]}"))

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Batch complete: {stats['posts_approved']} approved, "
                    f"{stats['posts_rejected']} rejected, {stats['posts_skipped']} skipped"
                )
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Error: {e}"))
            stats["errors"] = len(posts_map)

        return stats

    def parse_json_response(self, response):
        """Parse JSON response from LLM, handling markdown code blocks"""
        try:
            # Try to extract JSON from markdown code block
            if "```" in response:
                # Find JSON between code blocks
                import re

                match = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
                if match:
                    response = match.group(1)

            # Clean up response
            response = response.strip()

            return json.loads(response)
        except json.JSONDecodeError:
            return None
