import logging
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags

error_log = logging.getLogger("judge.errors")
debug_log = logging.getLogger("judge.debug")


def log_exception(msg):
    error_log.exception(msg)


def log_debug(category, data):
    debug_log.info(f"{category}: {data}")


def notify_problem_authors(
    problem, error_message, error_type="Checker Error", submission=None
):
    """
    Send email notification to problem authors when there's a checker error.

    Args:
        problem: Problem instance
        error_message: Error message to include in email
        error_type: Type of error (default: "Checker Error")
        submission: Submission instance that caused the error (optional)
    """
    if not problem or not problem.authors.exists():
        # Fallback to admin notification if no authors
        log_exception(
            f"Problem {problem.code if problem else 'Unknown'} {error_type}: {error_message}"
        )
        return

    # Get author emails
    author_emails = []
    for author in problem.authors.all():
        if author.user.email:
            author_emails.append(author.user.email)

    if not author_emails:
        # Fallback to admin notification if no author emails
        log_exception(f"Problem {problem.code} {error_type}: {error_message}")
        return

    # Email throttling - check cache to prevent spam
    from django.core.cache import cache

    throttle_key = f"problem_author_email_throttle:{problem.code}:{hash(error_message)}"

    # Check if we've already sent this error notification recently (within 1 hour)
    if cache.get(throttle_key):
        debug_log.info(f"Email throttled for problem {problem.code}: {error_type}")
        return

    # Set throttle cache for 1 hour
    cache.set(throttle_key, True, 3600)

    # Prepare email content
    subject = f"[LQDOJ] {error_type} in Problem {problem.code}"

    context = {
        "problem": problem,
        "error_message": error_message,
        "error_type": error_type,
        "site_name": getattr(settings, "SITE_NAME", "LQDOJ"),
        "problem_url": f"{getattr(settings, 'SITE_URL', '')}/problem/{problem.code}",
        "edit_url": f"{getattr(settings, 'SITE_URL', '')}/problem/{problem.code}/test_data",
        "protocol": "http",
        "domain": getattr(settings, "SITE_URL", "")
        .replace("http://", "")
        .replace("https://", ""),
    }

    # Add submission URL if submission is provided
    if submission:
        context["submission_url"] = (
            f"{getattr(settings, 'SITE_URL', '')}/submission/{submission.id}"
        )

    # Create email body
    html_message = render_to_string("judge/emails/problem_checker_error.html", context)
    plain_message = strip_tags(html_message)

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@lqdoj.edu.vn"),
            recipient_list=author_emails,
            html_message=html_message,
            fail_silently=False,
        )

        # Log successful notification
        debug_log.info(
            f"Notified problem authors for {problem.code}: {', '.join(author_emails)}"
        )

    except Exception as e:
        # If email fails, fall back to admin notification
        log_exception(f"Failed to notify problem authors for {problem.code}: {str(e)}")
        log_exception(f"Original error - {error_type}: {error_message}")
