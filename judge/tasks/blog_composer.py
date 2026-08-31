import logging
import re
import time

from django.conf import settings
from django.utils.translation import gettext as _

from celery import shared_task

from judge.blog_composer.cache import get_session, save_proposal, save_session
from judge.management.commands.generate_magazine_posts import (
    CONTEST_LINK_RE,
    Command as MagazineCommand,
)
from judge.markdown import markdown
from judge.models import BlogPost, Organization
from llm_service.config import get_config
from llm_service.llm_api import LLMService

logger = logging.getLogger(__name__)
CONTEST_KEY_TOKEN_RE = re.compile(r"\b[a-z0-9][a-z0-9_-]*\b", re.IGNORECASE)


def _current_draft(session, post):
    proposal = session.get("proposal")
    if proposal:
        return proposal
    if not post:
        return None
    return {
        "title": post.title,
        "summary": post.summary,
        "content": post.content,
    }


def _proposal_payload(generated, current_draft):
    return {
        "title": generated.title,
        "summary": generated.summary,
        "content": generated.content,
        "rendered_content": str(markdown(generated.content, lazy_load=False)),
    }


def _requested_public_contest(magazine, feedback):
    """Resolve an explicitly linked or named public contest before planning."""
    match = CONTEST_LINK_RE.search(feedback)
    if match:
        contest = (
            magazine._base_public_contest_queryset().filter(key=match.group(1)).first()
        )
        if contest:
            return contest

    candidate_keys = [token.lower() for token in CONTEST_KEY_TOKEN_RE.findall(feedback)]
    if not candidate_keys:
        return None
    contests = {
        contest.key: contest
        for contest in magazine._base_public_contest_queryset().filter(
            key__in=set(candidate_keys)
        )
    }
    return next((contests[key] for key in candidate_keys if key in contests), None)


def _select_composer_workflow(magazine, service, organization, feedback, post_id):
    """Give the composer every public-content tool before choosing a writer."""
    history = magazine._magazine_history(organization, exclude_post_id=post_id)
    read_codes = set()
    searched_keys = set()
    read_keys = set()
    response = magazine._call_agent_with_tools(
        service=service,
        prompt=f"""COMMUNITY_CONTEXT:
{magazine._organization_text(organization)}
ADMINISTRATOR_GUIDANCE:
{feedback}

Choose the best workflow for this request: topic, problem, or contest. Use tools
when a specific public problem or public contest would improve the draft.""",
        system_prompt="""You are planning a supervised LQDOJ community blog draft.
You can search public problems, read their statements, search public contests, and
read contest details. Before selecting a specific problem or contest, read its full
details. Return exactly JSON:
{"workflow": "topic"|"problem"|"contest", "code": ""|null, "key": ""|null}""",
        tools=(
            magazine._public_problem_tool_definitions()
            + magazine._public_contest_tool_definitions()
        ),
        tool_executables=(
            magazine._public_problem_tool_executables(organization, (), read_codes)
            + magazine._public_contest_tool_executables(
                history, searched_keys, read_keys
            )
        ),
    )
    plan = magazine._parse_review_response(response or "") or {}
    if plan.get("workflow") == "contest" and plan.get("key") in read_keys:
        contest = (
            magazine._base_public_contest_queryset().filter(key=plan["key"]).first()
        )
        if contest:
            return "contest", contest
    if plan.get("workflow") == "problem" and plan.get("code") in read_codes:
        candidate = magazine._public_problem_candidate_map(
            [plan["code"]], organization, ()
        ).get(plan["code"])
        if candidate:
            return "problem", candidate
    return "topic", None


@shared_task(bind=True)
def compose_blog_task(
    self,
    user_id,
    post_id,
    feedback,
    organization_id,
    initial_title,
    author_username="",
):
    session = get_session(user_id, post_id)
    post = BlogPost.objects.filter(id=post_id).first() if post_id else None
    organization = Organization.objects.filter(
        id=organization_id, is_community=True
    ).first()
    topic = (post.title if post else initial_title).strip()
    if not organization:
        logger.warning("Composer request is missing a community")
        return {"success": False}

    session["settings"] = {
        "organization_id": organization.id,
        "initial_title": initial_title,
        "author_username": author_username,
    }
    session["messages"].append(
        {"role": "user", "content": feedback, "timestamp": int(time.time())}
    )
    config = get_config()
    service = LLMService(
        api_key=config.api_key,
        bot_name=getattr(settings, "MAGAZINE_LLM_BOT", "Gemini-3.7-Flash"),
        sleep_time=config.sleep_time,
        timeout=240,
        feature="community_blog_composer",
        user_id=user_id,
        metadata={"post_id": post_id, "organization_id": organization_id},
    )

    def report_progress(stage, done, total):
        self.update_state(
            state="PROGRESS", meta={"stage": stage, "done": done, "total": total}
        )

    try:
        report_progress(_("Starting the magazine workflow"), 0, 5)
        current_draft = _current_draft(session, post)
        magazine = MagazineCommand()
        magazine.target_org = organization
        magazine._configure_llm_generation()
        report_progress(_("Choosing a writing workflow"), 1, 5)
        selected = _requested_public_contest(magazine, feedback)
        if selected:
            workflow = "contest"
        else:
            workflow, selected = _select_composer_workflow(
                magazine, service, organization, feedback, post_id
            )
        if workflow == "contest":
            generated = magazine._generate_contest_post(
                service,
                selected,
                administrator_guidance=feedback,
                per_problem_analysis=True,
            )
        elif workflow == "problem":
            generated = magazine._generate_problem_post(service, selected)
        else:
            if not topic:
                logger.warning("Composer topic workflow is missing a topic")
                return {"success": False}
            generated = magazine.generate_topic_post_with_feedback(
                service=service,
                topic=topic,
                org=organization,
                feedback=feedback,
                current_draft=current_draft,
                progress_callback=report_progress,
            )
        report_progress(_("Preparing the proposal"), 5, 5)
        proposal = save_proposal(
            user_id,
            post_id,
            _proposal_payload(generated, current_draft),
            session=session,
        )
        message = "Đã tạo bản nháp theo hướng dẫn và tiêu chuẩn chuyên mục."
        session["messages"].append(
            {"role": "assistant", "content": message, "timestamp": int(time.time())}
        )
        save_session(user_id, post_id, session)
        return {"success": True, "message": message, "proposal": proposal}
    except Exception:
        logger.exception("Blog composer failed for user %s", user_id)
        return {"success": False}
