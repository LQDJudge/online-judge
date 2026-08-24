"""
Django management command for auto-moderating community organizations using LLM
Usage: python manage.py auto_moderate [options]
"""

import json
import os
import re
import sys

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.utils import timezone

# Add llm_service to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", ".."))

from llm_service.config import get_config
from llm_service.llm_api import LLMService
from judge.models import (
    BlogPost,
    Comment,
    CommentModerationLog,
    Contest,
    OrganizationModerationLog,
    Organization,
    Problem,
    Solution,
    get_comment_context_details,
    hide_comment_for_moderation,
    mute_comment_author,
)
from chat_box.models import Message as ChatMessage, ChatModerationLog
from chat_box.views import hide_lobby_message, mute_chat_user

# Static system prompts for LLM caching
COMMENT_SYSTEM_PROMPT = """You are a comment moderator for an educational programming site used by primary, secondary, and high-school students. Review comments and decide the action for each.

Respond ONLY with valid JSON array: [{"id": <comment_id>, "action": "keep" or "hide" or "review" or "mute_temp" or "mute_perm", "reason": "<short reason>"}]

Moderation style:
- Keep the discussion positive, friendly, and safe, like a teacher supervising a relaxed student study space.
- Allow friendly off-topic comments, jokes, memes, sarcasm, playful banter, criticism, low-effort replies, and students making friends.
- Focus enforcement on genuinely harmful behavior, not on harmless noise or normal socializing.

Actions:
- "keep": Comment is acceptable. Keep it visible.
- "hide": Hide this single comment. Use for clear spam, scams, targeted insults, harassment, obscene content, unsafe links, or other isolated violations.
- "review": Leave the comment visible but flag it for human review. Use when the comment may be unsafe but context is ambiguous.
- "mute_temp": Hide this comment and temporarily mute the author. Use for repeated spam, repeated insults, disruptive behavior, or moderate harassment.
- "mute_perm": Hide this comment and permanently mute the author. Use only for severe harassment, hate speech, threats, doxxing, explicit sexual content, grooming, or dangerous abuse.

Be strict for harmful, abusive, bullying, insulting, obscene, adult/sexual, threatening, doxxing, discriminatory, scam, gambling, invalid/malicious, or other clearly unsafe content, in any language including Vietnamese, English, slang, and leetspeak.
Be tolerant of obvious jokes, memes, sarcasm, playful banter, mild profanity without a target, and ambiguous context. Do not punish a user for a single unclear comment.
Include a concise reason for every hide, review, or mute action. HIDE single comments for isolated clear violations. Use temporary mute for moderate repeated abuse. Use permanent mute only for severe abuse.
When in doubt, KEEP the comment. If a comment seems concerning but not clearly harmful, use REVIEW instead of hiding.

LINKS: Sharing links is allowed when the link appears to be valid and benign. KEEP links to contests, learning resources, LQDOJ organizations, study groups, Discord or other group chats, GitHub/docs, games, memes, or community events unless the message or destination is clearly harmful. Do not classify a link as promotional spam or a join-group violation solely because it invites users to a group, organization, Discord server, game, or community. Hide or mute link comments only when there is clear evidence of spam flooding, scams, gambling, adult content, malware/phishing, doxxing, hate/harassment, illegal activity, or another unsafe destination. If you cannot verify the destination and the surrounding comment is not clearly harmful, KEEP it.

IMAGES: Comments containing images (shown as [imageN] with an attached file) are normal in this community: users share screenshots, problem images, memes, and jokes. KEEP image comments unless the image is clearly harmful (nudity, gore, hate symbols, harassment, doxxing, scams, or illegal content). If you cannot see an image or it failed to load, always KEEP the comment."""

POST_SYSTEM_PROMPT = """You are a content moderator. Review blog posts and decide if each should be APPROVED, REJECTED, or SKIPPED.

Respond ONLY with valid JSON array: [{"id": <post_id>, "action": "approve" or "reject" or "skip"}]

APPROVE: on-topic, appropriate content.
REJECT only if clearly harmful: spam, hate speech, harassment, threats.
SKIP: uncertain, needs human review.
When in doubt, SKIP for human review."""

# User prompts with variable content
COMMENT_USER_PROMPT = """Site comments to review:
{comments}"""

POST_USER_PROMPT = """Community: {about}

Posts to review:
{posts}"""

CHAT_SYSTEM_PROMPT = """You are a chat lobby moderator for an educational programming site used by primary, secondary, and high-school students. Review messages and decide the action for each.

Respond ONLY with valid JSON array: [{"id": <message_id>, "action": "keep" or "hide" or "review" or "mute_temp" or "mute_perm", "reason": "<short reason>"}]

Moderation style:
- Keep the lobby positive, friendly, and safe, like a teacher supervising a relaxed student study space.
- Allow friendly off-topic chat, jokes, memes, sarcasm, playful banter, and students making friends.
- Focus enforcement on genuinely harmful behavior, not on harmless noise or normal socializing.

Actions:
- "hide": Hide this single message. Use for clear spam, scams, targeted insults, harassment, obscene content, unsafe links, or other isolated violations.
- "review": Leave the message visible but flag it for human review. Use when the message may be unsafe but context is ambiguous.
- "mute_temp": Hide ALL lobby messages from this user and temporarily mute them. Use for repeated spam, repeated insults, disruptive behavior, or moderate harassment.
- "mute_perm": Hide ALL lobby messages from this user and permanently mute them. Use for severe harassment, hate speech, threats, doxxing, explicit sexual content, grooming, or dangerous abuse.
- "keep": Message is acceptable. Keep it visible.

Be strict for harmful, abusive, bullying, insulting, obscene, adult/sexual, threatening, doxxing, discriminatory, scam, gambling, invalid/malicious, or other clearly unsafe content, in any language including Vietnamese, English, slang, and leetspeak.
Be tolerant of obvious jokes, memes, sarcasm, playful banter, mild profanity without a target, and ambiguous context. Do not punish a user for a single unclear message.
Include a concise reason for every hide, review, or mute action. HIDE single messages for isolated clear violations. Use temporary mute for moderate repeated abuse. Use permanent mute only for severe abuse.
When in doubt, KEEP the message. If a message seems concerning but not clearly harmful, use REVIEW instead of hiding.

LINKS: Sharing links is allowed when the link appears to be valid and benign. KEEP links to contests, learning resources, LQDOJ organizations, study groups, Discord or other group chats, GitHub/docs, games, memes, or community events unless the message or destination is clearly harmful. Do not classify a link as promotional spam or a join-group violation solely because it invites users to a group, organization, Discord server, game, or community. Hide or mute link messages only when there is clear evidence of spam flooding, scams, gambling, adult content, malware/phishing, doxxing, hate/harassment, illegal activity, or another unsafe destination. If you cannot verify the destination and the surrounding message is not clearly harmful, KEEP it.

IMAGES: Messages containing images (shown as [imageN] with an attached file) are normal in this community: users share screenshots, problem images, memes, and jokes. KEEP image messages unless the image is clearly harmful (nudity, gore, hate symbols, harassment, doxxing, scams, or illegal content). If you cannot see an image or it failed to load, always KEEP the message."""

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
            "--comment-window-minutes",
            type=int,
            default=60,
            help="Only review comments from this many recent minutes (default: 60)",
        )
        parser.add_argument(
            "--chat-only",
            action="store_true",
            help="Only moderate chat lobby messages",
        )
        parser.add_argument(
            "--chat-window-minutes",
            type=int,
            default=60,
            help="Only review chat messages from this many recent minutes (default: 60)",
        )

    def handle(self, *args, **options):
        # Get LLM settings
        api_key = getattr(settings, "POE_API_KEY", None)
        if not api_key:
            self.stderr.write(self.style.ERROR("POE_API_KEY not found in settings"))
            return

        try:
            config = get_config()
            bot_name = config.get_bot_name_for_moderation()
            self.llm_service = LLMService(
                api_key=api_key,
                bot_name=bot_name,
            )
            self.chat_llm_service = LLMService(
                api_key=config.api_key,
                bot_name=bot_name,
                sleep_time=config.sleep_time,
                timeout=config.timeout,
            )
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"Failed to initialize LLM service: {e}")
            )
            return

        self.dry_run = options["dry_run"]
        self.batch_size = options["batch_size"]
        self.comment_window_minutes = options["comment_window_minutes"]
        self.chat_window_minutes = options["chat_window_minutes"]

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
            "comments_reviewed": 0,
            "comments_muted": 0,
            "comments_kept": 0,
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "chat_hidden": 0,
            "chat_reviewed": 0,
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
            if not options["comments_only"]:
                # Get organizations for pending post moderation
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
                            self.style.SUCCESS(
                                f"Organization: {org.name} (ID: {org.id})"
                            )
                        )
                        self.stdout.write(f"{'='*60}")

                        org_stats = self.process_organization(org, options)
                        for key in total_stats:
                            total_stats[key] += org_stats.get(key, 0)
                else:
                    self.stdout.write(self.style.WARNING("No organizations found"))

            if not options["posts_only"]:
                self.stdout.write(f"\n{'='*60}")
                self.stdout.write(self.style.SUCCESS("Site-wide Comment Moderation"))
                self.stdout.write(f"{'='*60}")
                comment_stats = self.moderate_comments()
                for key in comment_stats:
                    total_stats[key] += comment_stats.get(key, 0)

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
        self.stdout.write(
            f"Comments flagged for review: {total_stats['comments_reviewed']}"
        )
        self.stdout.write(f"Comment users muted: {total_stats['comments_muted']}")
        self.stdout.write(f"Comments kept: {total_stats['comments_kept']}")
        self.stdout.write(f"Posts approved: {total_stats['posts_approved']}")
        self.stdout.write(f"Posts rejected: {total_stats['posts_rejected']}")
        self.stdout.write(f"Posts skipped: {total_stats['posts_skipped']}")
        self.stdout.write(f"Chat messages hidden: {total_stats['chat_hidden']}")
        self.stdout.write(
            f"Chat messages flagged for review: {total_stats['chat_reviewed']}"
        )
        self.stdout.write(f"Chat users muted: {total_stats['chat_muted']}")
        self.stdout.write(f"Chat messages kept: {total_stats['chat_kept']}")
        self.stdout.write(f"Errors: {total_stats['errors']}")

    def process_organization(self, org, options):
        stats = {
            "posts_approved": 0,
            "posts_rejected": 0,
            "posts_skipped": 0,
            "errors": 0,
        }

        about = org.about or "General community"

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
        If upload fails the original markdown is kept so the LLM knows an image
        was present but couldn't be loaded (preventing false hide/mute decisions).
        Returns (labeled_content, attachments).
        """
        attachments = []

        def replace(match):
            original = match.group(0)
            url = match.group(1)
            counter[0] += 1
            label = f"[image{counter[0]}]"
            attachment = self.llm_service.upload_file(url)
            if attachment:
                attachments.append(attachment)
                return label
            # Upload failed — keep original markdown so the LLM sees that
            # an image was shared and defaults to KEEP per the system prompt.
            return original

        labeled = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", replace, content)
        return labeled, attachments

    def _get_unreviewed_comments(self):
        cutoff = timezone.now() - timezone.timedelta(
            minutes=self.comment_window_minutes
        )
        allowed_content_type_ids = [
            ContentType.objects.get_for_model(model).id
            for model in (Problem, Contest, BlogPost, Solution)
        ]

        reviewed_comment_ids = CommentModerationLog.objects.filter(
            created_at__gte=cutoff
        ).values_list("comment_id", flat=True)

        candidate_pool = list(
            Comment.objects.filter(
                hidden=False,
                time__gte=cutoff,
                content_type_id__in=allowed_content_type_ids,
            )
            .exclude(id__in=reviewed_comment_ids)
            .select_related("author__user", "content_type")
            .order_by("id")[: self.batch_size]
        )
        return candidate_pool

    def moderate_comments(self):
        """Moderate recent site-wide comments (batched)."""
        stats = {
            "comments_hidden": 0,
            "comments_reviewed": 0,
            "comments_muted": 0,
            "comments_kept": 0,
            "errors": 0,
        }

        comments = self._get_unreviewed_comments()

        if not comments:
            self.stdout.write("  No comments to review")
            return stats

        self.stdout.write(f"  Reviewing {len(comments)} comments in one batch...")

        # Build batch prompt
        comments_text = []
        comments_map = {}
        attachments = []
        image_counter = [0]
        context_details = get_comment_context_details(comments)
        for comment in comments:
            author_name = comment.author.username if comment.author else "Anonymous"
            content = (comment.body or "").strip()
            if not content:
                continue
            comments_map[comment.id] = comment
            labeled, imgs = self._embed_images(content[:500], image_counter)
            attachments.extend(imgs)
            comments_text.append(
                "[Comment ID: %(id)s] by %(author)s\nContext: %(context)s\n%(body)s"
                % {
                    "id": comment.id,
                    "author": author_name,
                    "context": context_details[comment.id]["prompt_label"],
                    "body": labeled,
                }
            )

        if not comments_text:
            self.stdout.write("  No non-empty comments to review")
            return stats

        if attachments:
            self.stdout.write(f"  Uploaded {len(attachments)} image(s) to Poe")

        user_prompt = COMMENT_USER_PROMPT.format(
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
            processed_comment_ids = set()
            for result in results:
                comment_id = self._coerce_result_id(result.get("id"))
                action = result.get("action", "").lower()

                if comment_id not in comments_map:
                    stats["errors"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Ignoring result for unknown comment id: {result.get('id')}"
                        )
                    )
                    continue

                comment = comments_map[comment_id]
                processed_comment_ids.add(comment_id)
                author_name = comment.author.username if comment.author else "Anonymous"
                reason = (result.get("reason") or "").strip()
                if action in (
                    "hide",
                    "review",
                    "mute_temp",
                    "mute_temporary",
                    "mute_perm",
                    "mute_permanent",
                ):
                    reason = reason or "Automated moderation"

                if action in ("mute_perm", "mute_permanent"):
                    if not self.dry_run:
                        mute_comment_author(
                            comment,
                            reason=reason,
                            is_automated=True,
                            mute_type="permanent",
                        )
                    stats["comments_muted"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"    MUTED: {author_name} - {comment.body[:50]}..."
                        )
                    )

                elif action in ("mute_temp", "mute_temporary"):
                    if not self.dry_run:
                        mute_comment_author(
                            comment,
                            reason=reason,
                            is_automated=True,
                            mute_type="temporary",
                        )
                    stats["comments_muted"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"    TEMP MUTED: {author_name} - {comment.body[:50]}..."
                        )
                    )

                elif action == "hide":
                    if not self.dry_run:
                        hide_comment_for_moderation(
                            comment,
                            reason=reason,
                            is_automated=True,
                        )
                    stats["comments_hidden"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    HIDDEN: {author_name} - {comment.body[:50]}..."
                        )
                    )

                elif action == "review":
                    if not self.dry_run:
                        CommentModerationLog.log_action(
                            comment=comment,
                            action=CommentModerationLog.ACTION_REVIEW,
                            reason=reason,
                            is_automated=True,
                        )
                    stats["comments_reviewed"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    REVIEW: {author_name} - {comment.body[:50]}..."
                        )
                    )

                else:
                    if not self.dry_run:
                        CommentModerationLog.log_action(
                            comment=comment,
                            action=CommentModerationLog.ACTION_KEEP,
                            is_automated=True,
                        )
                    stats["comments_kept"] += 1

            missing_comment_ids = set(comments_map) - processed_comment_ids
            if missing_comment_ids:
                stats["errors"] += len(missing_comment_ids)
                self.stdout.write(
                    self.style.WARNING(
                        "  Missing moderation results for comments: %(ids)s"
                        % {
                            "ids": ", ".join(
                                str(i) for i in sorted(missing_comment_ids)
                            )
                        }
                    )
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Batch complete: {stats['comments_kept']} kept, "
                    f"{stats['comments_reviewed']} flagged, "
                    f"{stats['comments_hidden']} hidden, {stats['comments_muted']} muted"
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
            .exclude(authors__user__is_superuser=True)
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
        stats = {
            "chat_hidden": 0,
            "chat_reviewed": 0,
            "chat_muted": 0,
            "chat_kept": 0,
            "errors": 0,
        }

        cutoff = timezone.now() - timezone.timedelta(minutes=self.chat_window_minutes)

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
            response = self.chat_llm_service.call_llm(
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
            processed_message_ids = set()

            for result in results:
                msg_id = self._coerce_result_id(result.get("id"))
                action = result.get("action", "").lower()

                if msg_id not in messages_map:
                    stats["errors"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    Ignoring result for unknown message id: {result.get('id')}"
                        )
                    )
                    continue

                msg = messages_map[msg_id]
                processed_message_ids.add(msg_id)

                # Skip if this author was already muted in this batch
                if msg.author_id in muted_authors:
                    continue

                author_name = msg.author.user.username if msg.author else "Anonymous"

                reason = (result.get("reason") or "").strip()
                if action in (
                    "hide",
                    "review",
                    "mute",
                    "mute_perm",
                    "mute_permanent",
                    "mute_temp",
                    "mute_temporary",
                ):
                    reason = reason or "Automated moderation"

                if action in ("mute", "mute_perm", "mute_permanent"):
                    if not self.dry_run:
                        mute_chat_user(
                            msg,
                            is_automated=True,
                            reason=reason,
                            mute_type="permanent",
                        )
                    muted_authors.add(msg.author_id)
                    stats["chat_muted"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"    MUTED: {author_name} - {msg.body[:50]}..."
                        )
                    )

                elif action in ("mute_temp", "mute_temporary"):
                    if not self.dry_run:
                        mute_chat_user(
                            msg,
                            is_automated=True,
                            reason=reason,
                            mute_type="temporary",
                        )
                    muted_authors.add(msg.author_id)
                    stats["chat_muted"] += 1
                    self.stdout.write(
                        self.style.ERROR(
                            f"    TEMP MUTED: {author_name} - {msg.body[:50]}..."
                        )
                    )

                elif action == "hide":
                    if not self.dry_run:
                        hide_lobby_message(msg, is_automated=True, reason=reason)
                    stats["chat_hidden"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    HIDDEN: {author_name} - {msg.body[:50]}..."
                        )
                    )

                elif action == "review":
                    if not self.dry_run:
                        ChatModerationLog.log_action(
                            message=msg,
                            action="review",
                            reason=reason,
                            is_automated=True,
                        )
                    stats["chat_reviewed"] += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"    REVIEW: {author_name} - {msg.body[:50]}..."
                        )
                    )

                else:  # keep
                    if not self.dry_run:
                        ChatModerationLog.log_action(
                            message=msg, action="keep", is_automated=True
                        )
                    stats["chat_kept"] += 1

            missing_message_ids = set(messages_map) - processed_message_ids
            if missing_message_ids:
                stats["errors"] += len(missing_message_ids)
                self.stdout.write(
                    self.style.WARNING(
                        "  Missing moderation results for chat messages: %(ids)s"
                        % {
                            "ids": ", ".join(
                                str(i) for i in sorted(missing_message_ids)
                            )
                        }
                    )
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Batch complete: {stats['chat_kept']} kept, "
                    f"{stats['chat_reviewed']} flagged, "
                    f"{stats['chat_hidden']} hidden, {stats['chat_muted']} muted"
                )
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Error: {e}"))
            stats["errors"] = len(messages_map)

        return stats

    def _coerce_result_id(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
