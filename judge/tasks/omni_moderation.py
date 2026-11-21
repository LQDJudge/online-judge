"""OpenAI moderation for public lobby messages and comments."""

import base64
import hashlib
import logging
import math
import time
import uuid
from urllib.parse import urljoin, urlsplit

from django.contrib.sites.models import Site
from django.core.cache import caches
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import SHA256
from django.utils import timezone

from celery import shared_task

from chat_box.models import ChatModerationLog, Message
from judge.models import (
    AIUsageLog,
    BlogPost,
    Comment,
    CommentModerationLog,
    Contest,
    Problem,
    Solution,
)
from judge.moderation_images import (
    IMAGE_PATTERN,
    ModerationImageError,
    read_uploaded_moderation_image,
)
from judge.moderation_status import MuseStatus
from judge.moderation_visibility import set_moderated_visibility
from judge.utils.contribution import is_content_public
from llm_service.omni_moderation import OmniModerationError, moderate_content

__all__ = ["moderate_omni_item"]

logger = logging.getLogger(__name__)
COMMENT_TYPES = ("problem", "contest", "blogpost", "solution")
# Candidate thresholds; review against labelled production examples before release.
REVIEW_SCORE = 0.10
HIDE_SCORE = 0.75
HIDE_CATEGORIES = {
    "sexual/minors",
    "violence/graphic",
    "harassment/threatening",
    "hate/threatening",
}


def content_hash(body):
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def enqueue_omni(kind, object_id, body):
    try:
        app = moderate_omni_item.app
        transport_options = dict(app.conf.broker_transport_options or {})
        transport_options.update(
            socket_connect_timeout=1, socket_timeout=1, retry_on_timeout=False
        )
        with app.connection_for_write(
            connect_timeout=1, transport_options=transport_options
        ) as connection:
            connection.ensure_connection(max_retries=0)
            moderate_omni_item.apply_async(
                args=(kind, object_id, content_hash(body)),
                connection=connection,
                retry=False,
            )
    except Exception:
        logger.exception("Unable to queue Omni moderation for %s %s", kind, object_id)


def eligible(obj, kind, allow_hidden=False):
    if obj is None or (obj.hidden and not allow_hidden):
        return False
    if kind == "chat":
        return obj.kind == Message.Kind.USER and obj.room.singleton_key == "lobby"
    if kind == "comment":
        if obj.content_type.model == "blogpost":
            blog = obj.linked_object
            return bool(
                blog
                and blog.visible
                and not blog.is_organization_private
                and blog.publish_on <= timezone.now()
            )
        if obj.content_type.model == "solution":
            solution = obj.linked_object
            return bool(
                solution
                and solution.is_public
                and solution.problem_id
                and solution.problem.is_public
                and not solution.problem.is_organization_private
                and solution.publish_on <= timezone.now()
            )
        return obj.content_type.model in COMMENT_TYPES and is_content_public(
            obj.content_type, obj.object_id
        )
    return False


def reviewed(obj, kind, digest):
    if kind == "chat":
        return (
            ChatModerationLog.objects.filter(message_id=obj.pk)
            .exclude(muse_status__in=[MuseStatus.PENDING_OMNI, MuseStatus.SUPERSEDED])
            .exists()
        )
    return (
        CommentModerationLog.objects.filter(comment_id=obj.pk, content_hash=digest)
        .exclude(muse_status__in=[MuseStatus.PENDING_OMNI, MuseStatus.SUPERSEDED])
        .exists()
    )


def record_omni_pending(kind, obj):
    """Record the check in the same transaction as the content write."""
    if not eligible(obj, kind):
        return False
    digest = content_hash(obj.body)
    model = ChatModerationLog if kind == "chat" else CommentModerationLog
    field = "message" if kind == "chat" else "comment"
    query = {field + "_id": obj.pk}
    if kind == "comment":
        # An edit supersedes checks for earlier text.
        model.objects.filter(
            **query,
            muse_status__in=[MuseStatus.PENDING_OMNI, MuseStatus.PENDING_VISIBLE],
        ).exclude(content_hash=digest).update(muse_status=MuseStatus.SUPERSEDED)
        query["content_hash"] = digest
    statuses = set(
        model.objects.filter(**query)
        .exclude(muse_status=MuseStatus.SUPERSEDED)
        .values_list("muse_status", flat=True)
        .distinct()
    )
    if statuses:
        return statuses == {MuseStatus.PENDING_OMNI}
    kwargs = {field: obj}
    if kind == "comment":
        kwargs["content_hash"] = digest
    model.log_action(
        **kwargs, action="keep", is_automated=True, muse_status=MuseStatus.PENDING_OMNI
    )
    return True


def image_urls(body):
    paths = IMAGE_PATTERN.findall(body or "")
    if len(paths) > 4:
        return [], "too_many_images"
    if not paths:
        return [], None
    domain = Site.objects.get_current().domain
    urls = []
    image_error = None
    for path in paths:
        try:
            url = urljoin("https://%s/" % domain, path)
            parsed = urlsplit(url)
        except ValueError:
            image_error = "invalid_image_url"
            continue
        if parsed.scheme != "https" or not parsed.hostname:
            image_error = "invalid_image_url"
            continue
        try:
            uploaded = read_uploaded_moderation_image(path)
        except ModerationImageError:
            image_error = "image_storage_unavailable"
            continue
        if uploaded:
            data, mime = uploaded
            url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        urls.append(url)
    return urls, image_error


@shared_task(bind=True)
def moderate_omni_item(self, kind, object_id, expected_hash):
    # Recovery and old Celery retries can deliver the same case concurrently.
    # A short-lived shared lock avoids duplicate provider calls; the durable
    # pending record remains recoverable if a worker dies.
    key = "omni-moderation:%s:%s:%s" % (kind, object_id, expected_hash)
    token = uuid.uuid4().hex
    # Bypass the request-local L0 cache for this cross-worker lock.
    cache = caches["primary"]
    if not cache.add(key, token, timeout=300):
        return {"skipped": "already processing"}
    try:
        return _moderate_omni_item(kind, object_id, expected_hash)
    finally:
        if cache.get(key) == token:
            cache.delete(key)


def _moderate_omni_item(kind, object_id, expected_hash):
    if kind == "chat":
        queryset = Message.objects.select_related("room")
    elif kind == "comment":
        queryset = Comment.objects.select_related("content_type")
    else:
        return {"skipped": "unsupported"}
    obj = queryset.filter(pk=object_id).first()
    if not eligible(obj, kind) or content_hash(obj.body) != expected_hash:
        return {"skipped": "stale or private"}
    if reviewed(obj, kind, expected_hash):
        return {"skipped": "already reviewed"}
    started = time.monotonic()
    status = AIUsageLog.STATUS_ERROR
    scores = {}
    result = None
    image_incomplete = False
    error_category = None
    error_status_code = None
    try:
        images, error_category = image_urls(obj.body)
        image_incomplete = bool(error_category)
        try:
            result = moderate_content(obj.body or "", images)
        except OmniModerationError as exc:
            if not images or exc.status_code != 400:
                raise
            # A rejected image must not prevent the text being moderated.
            # Muse will check the image separately.
            error_category = "image_rejected"
            error_status_code = exc.status_code
            image_incomplete = True
            result = moderate_content(obj.body or "", [])
        raw_scores = result.get("category_scores", {})
        if isinstance(raw_scores, dict):
            scores = {
                name: value
                for name, value in raw_scores.items()
                if isinstance(name, str)
                and type(value) in (int, float)
                and 0 <= value <= 1
                and math.isfinite(value)
            }
        status = AIUsageLog.STATUS_SUCCESS
    except Exception as exc:
        error_category = (
            exc.category if isinstance(exc, OmniModerationError) else "unexpected_error"
        )
        error_status_code = (
            exc.status_code if isinstance(exc, OmniModerationError) else None
        )
        logger.warning(
            "Omni moderation failed for %s %s (%s)", kind, object_id, error_category
        )
        # Muse is already the durable second layer; do not create overlapping
        # Celery retries and periodic recovery attempts for a failing item.
        result = {"flagged": True, "categories": {}}
    finally:
        try:
            AIUsageLog.objects.create(
                feature="chat_moderation" if kind == "chat" else "comment_moderation",
                bot_name="omni-moderation-latest",
                status=status,
                duration_ms=(time.monotonic() - started) * 1000,
                input_chars=len(obj.body or ""),
                message_count=1,
                metadata={
                    "kind": kind,
                    "object_id": object_id,
                    "image_incomplete": image_incomplete,
                    "error_category": error_category,
                    "error_status_code": error_status_code,
                    "category_scores": scores,
                    "flagged": (
                        result.get("flagged") if isinstance(result, dict) else None
                    ),
                    "categories": (
                        result.get("categories", {}) if isinstance(result, dict) else {}
                    ),
                    "category_applied_input_types": (
                        result.get("category_applied_input_types", {})
                        if isinstance(result, dict)
                        else {}
                    ),
                },
            )
        except Exception:
            logger.exception("Unable to record Omni usage")
    with transaction.atomic():
        log_model = ChatModerationLog if kind == "chat" else CommentModerationLog
        field = "message" if kind == "chat" else "comment"
        query = {field + "_id": object_id, "muse_status": MuseStatus.PENDING_OMNI}
        if kind == "comment":
            query["content_hash"] = expected_hash
        pending = log_model.objects.select_for_update().filter(**query).first()
        current = queryset.select_for_update().filter(pk=object_id).first()
        if not eligible(current, kind) or content_hash(current.body) != expected_hash:
            return {"skipped": "changed"}
        if reviewed(current, kind, expected_hash):
            return {"skipped": "already reviewed"}
        flagged = result["flagged"]
        categories = result["categories"]
        applied = result.get("category_applied_input_types", {})
        high_risk = any(
            categories.get(name) is True
            and scores.get(name, 0) >= HIDE_SCORE
            and isinstance(applied, dict)
            and bool(applied.get(name))
            for name in HIDE_CATEGORIES
        )
        needs_muse = (
            status != AIUsageLog.STATUS_SUCCESS
            or image_incomplete
            or flagged
            or any(value >= REVIEW_SCORE for value in scores.values())
        )
        muse_status = (
            MuseStatus.PENDING_HIDDEN
            if high_risk
            else MuseStatus.PENDING_VISIBLE if needs_muse else MuseStatus.NONE
        )
        if high_risk:
            set_moderated_visibility(current, kind, True)
        if pending:
            pending.action = "review" if needs_muse else "keep"
            pending.reason = ""
            pending.muse_status = muse_status
            pending.save(update_fields=["action", "reason", "muse_status"])
        else:
            kwargs = {field: current}
            if kind == "comment":
                kwargs["content_hash"] = expected_hash
            log_model.log_action(
                **kwargs,
                action="review" if needs_muse else "keep",
                reason="",
                is_automated=True,
                muse_status=muse_status,
            )
    return {"flagged": flagged}


def public_comment_filter(prefix=""):
    targets = (
        (
            "problem",
            Problem.objects.filter(is_public=True, is_organization_private=False),
        ),
        (
            "contest",
            Contest.objects.filter(
                is_visible=True,
                is_private=False,
                is_organization_private=False,
                is_in_course=False,
            ),
        ),
        (
            "blogpost",
            BlogPost.objects.filter(
                visible=True,
                is_organization_private=False,
                publish_on__lte=timezone.now(),
            ),
        ),
        (
            "solution",
            Solution.objects.filter(
                is_public=True,
                problem__is_public=True,
                problem__is_organization_private=False,
                publish_on__lte=timezone.now(),
            ),
        ),
    )
    result = Q(pk__in=[])
    for name, queryset in targets:
        result |= Q(
            **{
                prefix + "content_type__model": name,
                prefix + "object_id__in": queryset.values("pk"),
            }
        )
    return result


def recover_omni_items(kind):
    model = ChatModerationLog if kind == "chat" else CommentModerationLog
    field = "message" if kind == "chat" else "comment"
    # Hidden content cannot be sent to Omni. Retire old pending records without
    # changing visibility; restored content can be rediscovered below.
    model.objects.filter(
        muse_status=MuseStatus.PENDING_OMNI, **{field + "__hidden": True}
    ).update(muse_status=MuseStatus.SUPERSEDED)
    pending = model.objects.filter(
        muse_status=MuseStatus.PENDING_OMNI, **{field + "__hidden": False}
    )
    if kind == "chat":
        pending = pending.filter(
            message__room__singleton_key="lobby", message__kind=Message.Kind.USER
        )
    else:
        pending = (
            pending.filter(public_comment_filter("comment__"))
            .annotate(current_hash=SHA256("comment__body"))
            .filter(content_hash=F("current_hash"))
        )
    pending = pending.select_related(field).order_by("created_at", "pk")
    count = 0
    for log in pending[:200]:
        obj = getattr(log, field)
        enqueue_omni(kind, obj.pk, obj.body)
        count += 1
    if count >= 200:
        return {"queued": count}

    # Superseded checks can become relevant again if an item is restored or an
    # edit is reverted. Unlike the legacy backfill below, these are durable
    # records and must remain recoverable even after the 120-minute window.
    lookup = {field + "_id": OuterRef(field + "_id")}
    if kind == "comment":
        lookup["content_hash"] = OuterRef("content_hash")
    current = model.objects.filter(**lookup).exclude(muse_status=MuseStatus.SUPERSEDED)
    newer_superseded = model.objects.filter(
        **lookup, muse_status=MuseStatus.SUPERSEDED, pk__gt=OuterRef("pk")
    )
    restored = (
        model.objects.filter(
            muse_status=MuseStatus.SUPERSEDED, **{field + "__hidden": False}
        )
        .annotate(has_current=Exists(current), has_newer=Exists(newer_superseded))
        .filter(has_current=False, has_newer=False)
    )
    if kind == "chat":
        restored = restored.filter(
            message__room__singleton_key="lobby", message__kind=Message.Kind.USER
        )
    else:
        restored = (
            restored.filter(public_comment_filter("comment__"))
            .annotate(current_hash=SHA256("comment__body"))
            .filter(content_hash=F("current_hash"))
        )
    for log in restored.select_related(field).order_by("created_at", "pk")[
        : 200 - count
    ]:
        obj = getattr(log, field)
        enqueue_omni(kind, obj.pk, obj.body)
        count += 1
    if count >= 200:
        return {"queued": count}

    cutoff = timezone.now() - timezone.timedelta(minutes=120)
    if kind == "chat":
        prior = ChatModerationLog.objects.filter(message_id=OuterRef("pk"))
        queryset = (
            Message.objects.filter(
                time__gte=cutoff,
                hidden=False,
                kind=Message.Kind.USER,
                room__singleton_key="lobby",
            )
            .annotate(done=Exists(prior))
            .filter(done=False)
            .select_related("room")
            .order_by("id")
        )
    else:
        prior = CommentModerationLog.objects.filter(
            comment_id=OuterRef("pk"), content_hash=OuterRef("digest")
        )
        queryset = (
            Comment.objects.filter(
                time__gte=cutoff, hidden=False, content_type__model__in=COMMENT_TYPES
            )
            .filter(public_comment_filter())
            .annotate(digest=SHA256("body"))
            .annotate(done=Exists(prior))
            .filter(done=False)
            .select_related("content_type")
            .order_by("-id")
        )
    for obj in queryset[: 200 - count]:
        enqueue_omni(kind, obj.pk, obj.body)
        count += 1
    return {"queued": count}
