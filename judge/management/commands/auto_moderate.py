"""
Django management command for auto-moderating community organizations using LLM
Usage: python manage.py auto_moderate [options]
"""

import sys
import os
import json
import time
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


COMMENT_MODERATION_PROMPT = """You are a content moderator for a community.

Community description:
{about}

Review this comment and decide if it should be HIDDEN or KEPT.

Comment by {author}:
---
{content}
---

Respond ONLY with valid JSON: {{"action": "hide" or "keep", "confidence": 0.0 to 1.0}}

HIDE: spam, offensive, harassment, off-topic.
KEEP: on-topic, constructive, neutral.
"""

POST_MODERATION_PROMPT = """You are a content moderator for a community.

Community description:
{about}

Review this pending blog post and decide if it should be APPROVED, REJECTED, or SKIPPED.

Title: {title}
Author: {author}
Content:
---
{content}
---

Respond ONLY with valid JSON: {{"action": "approve" or "reject" or "skip", "confidence": 0.0 to 1.0}}

APPROVE: on-topic, appropriate, valuable.
REJECT: spam, offensive, off-topic.
SKIP: uncertain, needs human review.
"""


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
            default=10,
            help="Number of items to process per batch (default: 10)",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=2.5,
            help="Seconds to sleep between LLM calls (default: 2.5)",
        )
        parser.add_argument(
            "--confidence-threshold",
            type=float,
            default=0.7,
            help="Minimum confidence to take action (default: 0.7)",
        )

    def handle(self, *args, **options):
        # Get LLM settings
        api_key = getattr(settings, "POE_API_KEY", None)
        if not api_key:
            self.stderr.write(self.style.ERROR("POE_API_KEY not found in settings"))
            return

        bot_name = getattr(settings, "POE_BOT_NAME", "Claude-3.7-Sonnet")
        sleep_time = options["sleep"]

        try:
            self.llm_service = LLMService(
                api_key=api_key,
                bot_name=bot_name,
                sleep_time=sleep_time,
            )
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"Failed to initialize LLM service: {e}")
            )
            return

        self.dry_run = options["dry_run"]
        self.confidence_threshold = options["confidence_threshold"]
        self.batch_size = options["batch_size"]
        self.sleep_time = sleep_time

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
        """Moderate comments on organization blog posts"""
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
        comments = (
            Comment.objects.filter(
                content_type=blog_content_type,
                object_id__in=blog_post_ids,
                hidden=False,
            )
            .exclude(id__in=already_reviewed)
            .select_related("author")
        )

        self.stdout.write(f"  Found {comments.count()} unhidden comments to review")

        for i, comment in enumerate(comments[: self.batch_size]):
            try:
                author_name = comment.author.username if comment.author else "Anonymous"
                content = comment.body or ""

                if not content.strip():
                    continue

                prompt = COMMENT_MODERATION_PROMPT.format(
                    about=about[:1000],  # Limit about text
                    author=author_name,
                    content=content[:2000],  # Limit content
                )

                self.stdout.write(f"    [{i+1}] Reviewing comment by {author_name}...")

                response = self.llm_service.call_llm(prompt)
                if not response:
                    self.stdout.write(
                        self.style.WARNING("      LLM returned no response")
                    )
                    stats["errors"] += 1
                    continue

                result = self.parse_json_response(response)
                if not result:
                    self.stdout.write(
                        self.style.WARNING(f"      Failed to parse: {response[:100]}")
                    )
                    stats["errors"] += 1
                    continue

                action = result.get("action", "").lower()
                confidence = float(result.get("confidence", 0))

                self.stdout.write(
                    f"      Action: {action}, Confidence: {confidence:.2f}"
                )

                if action == "hide" and confidence >= self.confidence_threshold:
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
                    self.stdout.write(self.style.WARNING("      -> HIDDEN"))
                else:
                    if not self.dry_run:
                        OrganizationModerationLog.log_action(
                            organization=org,
                            content_object=comment,
                            action="keep_comment",
                            is_automated=True,
                        )
                    stats["comments_kept"] += 1
                    self.stdout.write(self.style.SUCCESS("      -> KEPT"))

                time.sleep(self.sleep_time)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"      Error: {e}"))
                stats["errors"] += 1

        return stats

    def moderate_posts(self, org, about):
        """Moderate pending blog posts in organization"""
        stats = {
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "errors": 0,
        }

        # Get pending blog posts (visible=False, not rejected)
        pending_posts = BlogPost.objects.filter(
            organizations=org,
            visible=False,
            is_rejected=False,
        ).prefetch_related("authors")

        self.stdout.write(f"  Found {pending_posts.count()} pending posts")

        for i, post in enumerate(pending_posts[: self.batch_size]):
            try:
                authors = post.authors.all()
                author_name = (
                    ", ".join(a.username for a in authors) if authors else "Anonymous"
                )
                title = post.title or "Untitled"
                content = post.content or ""

                if not content.strip():
                    continue

                prompt = POST_MODERATION_PROMPT.format(
                    about=about[:1000],
                    title=title,
                    author=author_name,
                    content=content[:3000],  # Limit content
                )

                self.stdout.write(f"    [{i+1}] Reviewing post: {title[:40]}...")

                response = self.llm_service.call_llm(prompt)
                if not response:
                    self.stdout.write(
                        self.style.WARNING("      LLM returned no response")
                    )
                    stats["errors"] += 1
                    continue

                result = self.parse_json_response(response)
                if not result:
                    self.stdout.write(
                        self.style.WARNING(f"      Failed to parse: {response[:100]}")
                    )
                    stats["errors"] += 1
                    continue

                action = result.get("action", "").lower()
                confidence = float(result.get("confidence", 0))

                self.stdout.write(
                    f"      Action: {action}, Confidence: {confidence:.2f}"
                )

                if confidence >= self.confidence_threshold:
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
                        self.stdout.write(self.style.SUCCESS("      -> APPROVED"))

                    elif action == "reject":
                        if not self.dry_run:
                            OrganizationModerationLog.log_action(
                                organization=org,
                                content_object=post,
                                action="reject_post",
                                is_automated=True,
                            )
                            post.is_rejected = True
                            post.save(update_fields=["is_rejected"])
                        stats["posts_rejected"] += 1
                        self.stdout.write(self.style.ERROR("      -> REJECTED"))

                    elif action == "skip":
                        if not self.dry_run:
                            OrganizationModerationLog.log_action(
                                organization=org,
                                content_object=post,
                                action="skip",
                                is_automated=True,
                            )
                        stats["posts_skipped"] += 1
                        self.stdout.write(self.style.WARNING("      -> SKIPPED"))
                else:
                    # Low confidence - skip
                    stats["posts_skipped"] += 1
                    self.stdout.write("      -> SKIPPED (low confidence)")

                time.sleep(self.sleep_time)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"      Error: {e}"))
                stats["errors"] += 1

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
