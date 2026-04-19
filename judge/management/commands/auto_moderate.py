"""
Django management command for auto-moderating community organizations using LLM
Usage: python manage.py auto_moderate [options]
"""

import sys
import os
import json
import re
from django.core.management.base import BaseCommand
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

# Add llm_service to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", ".."))

from llm_service.llm_api import LLMService
from judge.models import (
    Organization,
    BlogPost,
    Comment,
    OrganizationModerationLog,
)
from chat_box.models import Message as ChatMessage, ChatModerationLog
from chat_box.views import hide_lobby_message, mute_chat_user

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

CHAT_SYSTEM_PROMPT = """You are a chat lobby moderator. Review messages and decide the action for each.

Respond ONLY with valid JSON array: [{"id": <message_id>, "action": "hide" or "mute" or "keep"}]

Actions:
- "hide": Hide this single message. Use for: spam, off-topic advertising, mildly inappropriate content.
- "mute": Hide ALL lobby messages from this user AND mute them from future lobby posting. Use for: severe harassment, hate speech, threats, doxxing, repeated spam from same user.
- "keep": Message is acceptable. Keep it visible.

HIDE single messages for isolated violations. MUTE only for severe or repeated abuse.
When in doubt, KEEP the message."""

CHAT_USER_PROMPT = """Chat lobby messages to review:
{messages}"""


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
        parser.add_argument(
            "--chat-only",
            action="store_true",
            help="Only moderate chat lobby messages",
        )

    def handle(self, *args, **options):
        # Get LLM settings
        api_key = getattr(settings, "POE_API_KEY", None)
        if not api_key:
            self.stderr.write(self.style.ERROR("POE_API_KEY not found in settings"))
            return

        bot_name = getattr(settings, "POE_BOT_NAME", "Gemini-3-Flash")

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

        if options["chat_only"] and (options["comments_only"] or options["posts_only"]):
            self.stderr.write(
                self.style.ERROR(
                    "--chat-only cannot be combined with --comments-only or --posts-only"
                )
            )
            return

        if self.dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        total_stats = {
            "comments_hidden": 0,
            "comments_kept": 0,
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "chat_hidden": 0,
            "chat_muted": 0,
            "chat_kept": 0,
            "errors": 0,
        }

        if options["chat_only"]:
            # Only moderate chat lobby
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(self.style.SUCCESS("Chat Lobby Moderation"))
            self.stdout.write(f"{'='*60}")
            chat_stats = self.moderate_chat()
            for key in chat_stats:
                total_stats[key] += chat_stats.get(key, 0)
        else:
            # Get organizations
            if options["org_ids"]:
                org_ids = [int(x.strip()) for x in options["org_ids"].split(",")]
                organizations = Organization.objects.filter(id__in=org_ids)
            else:
                organizations = Organization.objects.filter(is_community=True)

            if organizations.exists():
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Processing {organizations.count()} organization(s)"
                    )
                )

                for org in organizations:
                    self.stdout.write(f"\n{'='*60}")
                    self.stdout.write(
                        self.style.SUCCESS(f"Organization: {org.name} (ID: {org.id})")
                    )
                    self.stdout.write(f"{'='*60}")

                    org_stats = self.process_organization(org, options)
                    for key in total_stats:
                        total_stats[key] += org_stats.get(key, 0)
            else:
                self.stdout.write(self.style.WARNING("No organizations found"))

            # Also moderate chat unless filtered to comments/posts only
            if not options["comments_only"] and not options["posts_only"]:
                self.stdout.write(f"\n{'='*60}")
                self.stdout.write(self.style.SUCCESS("Chat Lobby Moderation"))
                self.stdout.write(f"{'='*60}")
                chat_stats = self.moderate_chat()
                for key in chat_stats:
                    total_stats[key] += chat_stats.get(key, 0)

        # Print summary
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(self.style.SUCCESS("SUMMARY"))
        self.stdout.write(f"{'='*60}")
        self.stdout.write(f"Comments hidden: {total_stats['comments_hidden']}")
        self.stdout.write(f"Comments kept: {total_stats['comments_kept']}")
        self.stdout.write(f"Posts approved: {total_stats['posts_approved']}")
        self.stdout.write(f"Posts rejected: {total_stats['posts_rejected']}")
        self.stdout.write(f"Posts skipped: {total_stats['posts_skipped']}")
        self.stdout.write(f"Chat messages hidden: {total_stats['chat_hidden']}")
        self.stdout.write(f"Chat users muted: {total_stats['chat_muted']}")
        self.stdout.write(f"Chat messages kept: {total_stats['chat_kept']}")
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

    def _embed_images(self, content, counter):
        """Replace markdown images with [imageN] labels and upload to Poe.

        counter is a mutable list [n] shared across items in a batch so labels
        are unique within the full prompt.
        Returns (labeled_content, attachments).
        """
        attachments = []

        def replace(match):
            url = match.group(1)
            counter[0] += 1
            label = f"[image{counter[0]}]"
            attachment = self.llm_service.upload_file(url)
            if attachment:
                attachments.append(attachment)
            return label

        labeled = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", replace, content)
        return labeled, attachments

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
        attachments = []
        image_counter = [0]
        for comment in comments:
            author_name = comment.author.username if comment.author else "Anonymous"
            content = (comment.body or "").strip()
            if not content:
                continue
            comments_map[comment.id] = comment
            labeled, imgs = self._embed_images(content[:500], image_counter)
            attachments.extend(imgs)
            comments_text.append(
                f"[Comment ID: {comment.id}] by {author_name}:\n{labeled}"
            )

        if not comments_text:
            self.stdout.write("  No non-empty comments to review")
            return stats

        if attachments:
            self.stdout.write(f"  Uploaded {len(attachments)} image(s) to Poe")

        user_prompt = COMMENT_USER_PROMPT.format(
            about=about[:1000],
            comments="\n\n---\n\n".join(comments_text),
        )

        try:
            response = self.llm_service.call_llm(
                user_prompt,
                system_prompt=COMMENT_SYSTEM_PROMPT,
                attachments=attachments,
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

        # Get post IDs already reviewed (in moderation log) to avoid re-evaluating skipped posts
        blog_post_content_type = ContentType.objects.get_for_model(BlogPost)
        already_reviewed = OrganizationModerationLog.objects.filter(
            organization=org,
            content_type=blog_post_content_type,
        ).values_list("object_id", flat=True)

        # Get pending blog posts (visible=False, not rejected), excluding already reviewed
        pending_posts = list(
            BlogPost.objects.filter(
                organizations=org,
                visible=False,
                is_rejected=False,
            )
            .exclude(id__in=already_reviewed)
            .prefetch_related("authors")[: self.batch_size]
        )

        if not pending_posts:
            self.stdout.write("  No pending posts to review")
            return stats

        self.stdout.write(f"  Reviewing {len(pending_posts)} posts in one batch...")

        # Build batch prompt
        posts_text = []
        posts_map = {}
        attachments = []
        image_counter = [0]
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
            labeled, imgs = self._embed_images(content[:1000], image_counter)
            attachments.extend(imgs)
            posts_text.append(
                f"[Post ID: {post.id}] Title: {title}\nAuthor: {author_name}\nContent:\n{labeled}"
            )

        if not posts_text:
            self.stdout.write("  No non-empty posts to review")
            return stats

        if attachments:
            self.stdout.write(f"  Uploaded {len(attachments)} image(s) to Poe")

        user_prompt = POST_USER_PROMPT.format(
            about=about[:1000],
            posts="\n\n---\n\n".join(posts_text),
        )

        try:
            response = self.llm_service.call_llm(
                user_prompt, system_prompt=POST_SYSTEM_PROMPT, attachments=attachments
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

    def moderate_chat(self):
        """Moderate lobby chat messages (batched)"""
        stats = {"chat_hidden": 0, "chat_muted": 0, "chat_kept": 0, "errors": 0}

        # Only review messages from the last hour
        cutoff = timezone.now() - timezone.timedelta(hours=1)

        # Get message IDs already reviewed (within the same time window)
        already_reviewed = ChatModerationLog.objects.filter(
            created_at__gte=cutoff
        ).values_list("message_id", flat=True)

        # Get unhidden lobby messages not yet reviewed, within cutoff
        messages = list(
            ChatMessage.objects.filter(room=None, hidden=False, time__gte=cutoff)
            .exclude(id__in=already_reviewed)
            .select_related("author__user")
            .order_by("id")[: self.batch_size]
        )

        if not messages:
            self.stdout.write("  No chat messages to review")
            return stats

        self.stdout.write(f"  Reviewing {len(messages)} chat messages...")

        # Build batch prompt
        messages_text = []
        messages_map = {}
        attachments = []
        image_counter = [0]
        for msg in messages:
            author_name = msg.author.user.username if msg.author else "Anonymous"
            body = (msg.body or "").strip()
            if not body:
                continue
            messages_map[msg.id] = msg
            labeled, imgs = self._embed_images(body, image_counter)
            attachments.extend(imgs)
            messages_text.append(f"[Message ID: {msg.id}] by {author_name}:\n{labeled}")

        if not messages_text:
            self.stdout.write("  No non-empty chat messages to review")
            return stats

        if attachments:
            self.stdout.write(f"  Uploaded {len(attachments)} image(s) to Poe")

        user_prompt = CHAT_USER_PROMPT.format(
            messages="\n\n---\n\n".join(messages_text),
        )

        try:
            response = self.llm_service.call_llm(
                user_prompt,
                system_prompt=CHAT_SYSTEM_PROMPT,
                attachments=attachments,
            )
            if not response:
                self.stdout.write(self.style.WARNING("  LLM returned no response"))
                stats["errors"] = len(messages_map)
                return stats

            results = self.parse_json_response(response)
            if not results or not isinstance(results, list):
                self.stdout.write(
                    self.style.WARNING(f"  Failed to parse response: {response[:200]}")
                )
                stats["errors"] = len(messages_map)
                return stats

            # Track muted authors to skip redundant processing
            muted_authors = set()

            for result in results:
                msg_id = result.get("id")
                action = result.get("action", "").lower()

                if msg_id not in messages_map:
                    continue

                msg = messages_map[msg_id]

                # Skip if this author was already muted in this batch
                if msg.author_id in muted_authors:
                    continue

                author_name = msg.author.user.username if msg.author else "Anonymous"

                if action == "mute":
                    if not self.dry_run:
                        mute_chat_user(msg, is_automated=True)
                    muted_authors.add(msg.author_id)
                    stats["chat_muted"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"    MUTED: {author_name} - {msg.body[:50]}..."
                        )
                    )

                elif action == "hide":
                    if not self.dry_run:
                        hide_lobby_message(msg, is_automated=True)
                    stats["chat_hidden"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    HIDDEN: {author_name} - {msg.body[:50]}..."
                        )
                    )

                else:  # keep
                    if not self.dry_run:
                        ChatModerationLog.log_action(
                            message=msg, action="keep", is_automated=True
                        )
                    stats["chat_kept"] += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Batch complete: {stats['chat_kept']} kept, "
                    f"{stats['chat_hidden']} hidden, {stats['chat_muted']} muted"
                )
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Error: {e}"))
            stats["errors"] = len(messages_map)

        return stats

    def parse_json_response(self, response):
        """Parse JSON response from LLM, handling markdown code blocks"""
        try:
            # Try to extract JSON from markdown code block
            if "```" in response:
                # Find JSON between code blocks
                match = re.search(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
                if match:
                    response = match.group(1)

            # Clean up response
            response = response.strip()

            return json.loads(response)
        except json.JSONDecodeError:
            return None
