from django.conf import settings
from django.conf.urls import include
from django.urls import re_path, path
from django.conf.urls.static import static as url_static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.sitemaps.views import sitemap
from django.http import Http404, HttpResponsePermanentRedirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import RedirectView

import chat_box.views as chat
from judge import authentication
from judge.forms import CustomAuthenticationForm
from judge.sitemap import (
    BlogPostSitemap,
    ContestSitemap,
    HomePageSitemap,
    OrganizationSitemap,
    ProblemSitemap,
    SolutionSitemap,
    UrlSitemap,
    UserSitemap,
)
from judge.views import (
    TitledTemplateView,
    about,
    api,
    blog,
    comment,
    contests,
    language,
    license,
    mailgun,
    markdown_editor,
    notification,
    organization,
    preview,
    problem,
    problem_manage,
    ranked_submission,
    register,
    stats,
    status,
    submission,
    tasks,
    ticket,
    totp,
    user,
    volunteer,
    pagevote,
    bookmark,
    widgets,
    internal,
    resolver,
    course,
    email,
    custom_file_upload,
)
from judge.views.problem_data import (
    ProblemDataView,
    ProblemSubmissionDiff,
    problem_data_file,
    problem_init_view,
    ProblemZipUploadView,
)
from judge.views.register import ActivationView, RegistrationView
from judge.views.select2 import (
    AssigneeSelect2View,
    ChatUserSearchSelect2View,
    ContestSelect2View,
    ContestUserSearchSelect2View,
    OrganizationSelect2View,
    ProblemSelect2View,
    TicketUserSelect2View,
    UserSearchSelect2View,
    UserSelect2View,
    ProblemAuthorSearchSelect2View,
)
from judge.views.test_formatter import test_formatter

admin.autodiscover()

register_patterns = [
    re_path(
        r"^activate/complete/$",
        TitledTemplateView.as_view(
            template_name="registration/activation_complete.html",
            title="Activation Successful!",
        ),
        name="registration_activation_complete",
    ),
    # Activation keys get matched by \w+ instead of the more specific
    # [a-fA-F0-9]{40} because a bad activation key should still get to the view;
    # that way it can return a sensible "invalid key" message instead of a
    # confusing 404.
    re_path(
        r"^activate/(?P<activation_key>\w+)/$",
        ActivationView.as_view(title=_("Activation key invalid")),
        name="registration_activate",
    ),
    re_path(
        r"^register/$",
        RegistrationView.as_view(title=_("Register")),
        name="registration_register",
    ),
    re_path(
        r"^register/complete/$",
        TitledTemplateView.as_view(
            template_name="registration/registration_complete.html",
            title=_("Registration Completed"),
        ),
        name="registration_complete",
    ),
    re_path(
        r"^register/closed/$",
        TitledTemplateView.as_view(
            template_name="registration/registration_closed.html",
            title=_("Registration not allowed"),
        ),
        name="registration_disallowed",
    ),
    re_path(
        r"^login/$",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            extra_context={"title": _("Login")},
            authentication_form=CustomAuthenticationForm,
            redirect_authenticated_user=True,
        ),
        name="auth_login",
    ),
    re_path(r"^logout/$", user.UserLogoutView.as_view(), name="auth_logout"),
    re_path(
        r"^password/change/$",
        authentication.CustomPasswordChangeView.as_view(),
        name="password_change",
    ),
    re_path(
        r"^password/change/done/$",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html",
        ),
        name="password_change_done",
    ),
    re_path(
        r"^password/reset/$",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset.html",
            html_email_template_name="registration/password_reset_email.html",
            email_template_name="registration/password_reset_email.txt",
        ),
        name="password_reset",
    ),
    re_path(
        r"^password/reset/confirm/(?P<uidb64>[0-9A-Za-z]+)-(?P<token>.+)/$",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
        ),
        name="password_reset_confirm",
    ),
    re_path(
        r"^password/reset/complete/$",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    re_path(
        r"^password/reset/done/$",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    re_path(r"^email/change/$", email.email_change_view, name="email_change"),
    re_path(
        r"^email/change/verify/(?P<uidb64>[0-9A-Za-z]+)-(?P<token>.+)/$",
        email.verify_email_view,
        name="email_change_verify",
    ),
    re_path(
        r"^email/change/pending$",
        email.email_change_pending_view,
        name="email_change_pending",
    ),
    re_path(r"^social/error/$", register.social_auth_error, name="social_auth_error"),
    re_path(r"^2fa/$", totp.TOTPLoginView.as_view(), name="login_2fa"),
    re_path(r"^2fa/enable/$", totp.TOTPEnableView.as_view(), name="enable_2fa"),
    re_path(r"^2fa/disable/$", totp.TOTPDisableView.as_view(), name="disable_2fa"),
]


def exception(request):
    if not request.user.is_superuser:
        raise Http404()
    raise RuntimeError("@Xyene asked me to cause this")


def paged_list_view(view, name, **kwargs):
    return include(
        [
            re_path(r"^$", view.as_view(**kwargs), name=name),
            re_path(r"^(?P<page>\d+)$", view.as_view(**kwargs), name=name),
        ]
    )


urlpatterns = [
    re_path("", include("pagedown.urls")),
    re_path(
        r"^$",
        blog.PostList.as_view(template_name="home.html", title=_("Home")),
        kwargs={"page": 1},
        name="home",
    ),
    re_path(r"^500/$", exception),
    re_path(r"^toggle_darkmode/$", user.toggle_darkmode, name="toggle_darkmode"),
    re_path(r"^admin/", admin.site.urls),
    re_path(r"^i18n/", include("django.conf.urls.i18n")),
    re_path(r"^accounts/", include(register_patterns)),
    re_path(r"^", include("social_django.urls")),
    re_path(
        r"^feed/",
        include(
            [
                re_path(r"^tickets/$", blog.TicketFeed.as_view(), name="ticket_feed"),
                re_path(
                    r"^comments/$", blog.CommentFeed.as_view(), name="comment_feed"
                ),
            ]
        ),
    ),
    re_path(r"^problems/", paged_list_view(problem.ProblemList, "problem_list")),
    re_path(
        r"^problems/random/$", problem.RandomProblem.as_view(), name="problem_random"
    ),
    re_path(
        r"^problems/feed/$",
        problem.ProblemFeed.as_view(feed_type="for_you"),
        name="problem_feed",
    ),
    re_path(
        r"^problems/feed/new/$",
        problem.ProblemFeed.as_view(feed_type="new"),
        name="problem_feed_new",
    ),
    re_path(
        r"^problems/feed/volunteer/$",
        problem.ProblemFeed.as_view(feed_type="volunteer"),
        name="problem_feed_volunteer",
    ),
    re_path(
        r"^problem/(?P<problem>[^/]+)",
        include(
            [
                re_path(r"^$", problem.ProblemDetail.as_view(), name="problem_detail"),
                re_path(
                    r"^/editorial$",
                    problem.ProblemSolution.as_view(),
                    name="problem_editorial",
                ),
                re_path(r"^/raw$", problem.ProblemRaw.as_view(), name="problem_raw"),
                re_path(
                    r"^/pdf$", problem.ProblemPdfView.as_view(), name="problem_pdf"
                ),
                re_path(
                    r"^/pdf/(?P<language>[a-z-]+)$",
                    problem.ProblemPdfView.as_view(),
                    name="problem_pdf",
                ),
                re_path(
                    r"^/pdf_description$",
                    problem.ProblemPdfDescriptionView.as_view(),
                    name="problem_pdf_description",
                ),
                re_path(
                    r"^/clone", problem.ProblemClone.as_view(), name="problem_clone"
                ),
                re_path(r"^/submit$", problem.problem_submit, name="problem_submit"),
                re_path(
                    r"^/resubmit/(?P<submission>\d+)$",
                    problem.problem_submit,
                    name="problem_submit",
                ),
                re_path(
                    r"^/rank/",
                    paged_list_view(
                        ranked_submission.RankedSubmissions, "ranked_submissions"
                    ),
                ),
                re_path(
                    r"^/submissions/",
                    paged_list_view(
                        submission.ProblemSubmissions, "chronological_submissions"
                    ),
                ),
                re_path(
                    r"^/submissions/(?P<user>\w+)/",
                    paged_list_view(
                        submission.UserProblemSubmissions, "user_submissions"
                    ),
                ),
                re_path(
                    r"^/$",
                    lambda _, problem: HttpResponsePermanentRedirect(
                        reverse("problem_detail", args=[problem])
                    ),
                ),
                re_path(
                    r"^/test_data$", ProblemDataView.as_view(), name="problem_data"
                ),
                re_path(
                    r"^/test_data/init$", problem_init_view, name="problem_data_init"
                ),
                re_path(
                    r"^/test_data/diff$",
                    ProblemSubmissionDiff.as_view(),
                    name="problem_submission_diff",
                ),
                re_path(
                    r"^/test_data/upload$",
                    ProblemZipUploadView.as_view(),
                    name="problem_zip_upload",
                ),
                re_path(
                    r"^/data/(?P<path>.+)$", problem_data_file, name="problem_data_file"
                ),
                re_path(
                    r"^/tickets$",
                    ticket.ProblemTicketListView.as_view(),
                    name="problem_ticket_list",
                ),
                re_path(
                    r"^/tickets/new$",
                    ticket.NewProblemTicketView.as_view(),
                    name="new_problem_ticket",
                ),
                re_path(
                    r"^/manage/submission",
                    include(
                        [
                            re_path(
                                "^$",
                                problem_manage.ManageProblemSubmissionView.as_view(),
                                name="problem_manage_submissions",
                            ),
                            re_path(
                                "^/action$",
                                problem_manage.ActionSubmissionsView.as_view(),
                                name="problem_submissions_action",
                            ),
                            re_path(
                                "^/action/preview$",
                                problem_manage.PreviewActionSubmissionsView.as_view(),
                                name="problem_submissions_rejudge_preview",
                            ),
                            re_path(
                                "^/rejudge/success/(?P<task_id>[A-Za-z0-9-]*)$",
                                problem_manage.rejudge_success,
                                name="problem_submissions_rejudge_success",
                            ),
                            re_path(
                                "^/rescore/all$",
                                problem_manage.RescoreAllSubmissionsView.as_view(),
                                name="problem_submissions_rescore_all",
                            ),
                            re_path(
                                "^/rescore/success/(?P<task_id>[A-Za-z0-9-]*)$",
                                problem_manage.rescore_success,
                                name="problem_submissions_rescore_success",
                            ),
                        ]
                    ),
                ),
            ]
        ),
    ),
    re_path(
        r"^submissions/", paged_list_view(submission.AllSubmissions, "all_submissions")
    ),
    re_path(
        r"^submissions/user/(?P<user>\w+)/",
        paged_list_view(submission.AllUserSubmissions, "all_user_submissions"),
    ),
    re_path(
        r"^submissions/friends/",
        paged_list_view(submission.AllFriendSubmissions, "all_friend_submissions"),
    ),
    re_path(
        r"^src/(?P<submission>\d+)/raw$",
        submission.SubmissionSourceRaw.as_view(),
        name="submission_source_raw",
    ),
    re_path(
        r"^submission/(?P<submission>\d+)",
        include(
            [
                re_path(
                    r"^$",
                    submission.SubmissionStatus.as_view(),
                    name="submission_status",
                ),
                re_path(
                    r"^/abort$", submission.abort_submission, name="submission_abort"
                ),
            ]
        ),
    ),
    re_path(
        r"^test_formatter/",
        include(
            [
                re_path(
                    r"^$",
                    login_required(test_formatter.TestFormatter.as_view()),
                    name="test_formatter",
                ),
                re_path(
                    r"^edit_page$",
                    login_required(test_formatter.EditTestFormatter.as_view()),
                    name="test_formatter_edit",
                ),
                re_path(
                    r"^download_page$",
                    login_required(test_formatter.DownloadTestFormatter.as_view()),
                    name="test_formatter_download",
                ),
            ]
        ),
    ),
    re_path(
        r"^markdown_editor/",
        markdown_editor.MarkdownEditor.as_view(),
        name="markdown_editor",
    ),
    re_path(
        r"^submission_source_file/(?P<filename>(\w|\.)+)",
        submission.SubmissionSourceFileView.as_view(),
        name="submission_source_file",
    ),
    re_path(
        r"^users/",
        include(
            [
                re_path(r"^$", user.users, name="user_list"),
                re_path(
                    r"^(?P<page>\d+)$",
                    lambda request, page: HttpResponsePermanentRedirect(
                        "%s?page=%s" % (reverse("user_list"), page)
                    ),
                ),
                re_path(
                    r"^find$", user.user_ranking_redirect, name="user_ranking_redirect"
                ),
            ]
        ),
    ),
    re_path(r"^user$", user.UserAboutPage.as_view(), name="user_page"),
    re_path(r"^edit/profile/$", user.edit_profile, name="user_edit_profile"),
    re_path(r"^user/bookmarks", user.UserBookMarkPage.as_view(), name="user_bookmark"),
    re_path(
        r"^user/(?P<user>\w+)",
        include(
            [
                re_path(r"^$", user.UserAboutPage.as_view(), name="user_page"),
                re_path(
                    r"^/solved",
                    include(
                        [
                            re_path(
                                r"^$",
                                user.UserProblemsPage.as_view(),
                                name="user_problems",
                            ),
                            re_path(
                                r"/ajax$",
                                user.UserPerformancePointsAjax.as_view(),
                                name="user_pp_ajax",
                            ),
                        ]
                    ),
                ),
                re_path(
                    r"^/submissions/",
                    paged_list_view(
                        submission.AllUserSubmissions, "all_user_submissions_old"
                    ),
                ),
                re_path(
                    r"^/submissions/",
                    lambda _, user: HttpResponsePermanentRedirect(
                        reverse("all_user_submissions", args=[user])
                    ),
                ),
                re_path(
                    r"^/toggle_follow/", user.toggle_follow, name="user_toggle_follow"
                ),
                re_path(
                    r"^/$",
                    lambda _, user: HttpResponsePermanentRedirect(
                        reverse("user_page", args=[user])
                    ),
                ),
            ]
        ),
    ),
    re_path(r"^pagevotes/vote/$", pagevote.vote_page, name="pagevote_vote"),
    re_path(r"^bookmarks/dobookmark/$", bookmark.dobookmark_page, name="dobookmark"),
    re_path(
        r"^bookmarks/undobookmark/$", bookmark.undobookmark_page, name="undobookmark"
    ),
    re_path(r"^comments/upvote/$", comment.upvote_comment, name="comment_upvote"),
    re_path(r"^comments/downvote/$", comment.downvote_comment, name="comment_downvote"),
    re_path(r"^comments/hide/$", comment.comment_hide, name="comment_hide"),
    re_path(r"^comments/post/$", comment.post_comment, name="comment_post"),
    re_path(r"^comments/get_comments/$", comment.get_comments, name="get_comments"),
    re_path(
        r"^comments/get_replies/$", comment.get_replies, name="comment_get_replies"
    ),
    re_path(
        r"^comments/(?P<id>\d+)/",
        include(
            [
                re_path(r"^edit$", comment.CommentEdit.as_view(), name="comment_edit"),
                re_path(
                    r"^history/ajax$",
                    comment.CommentRevisionAjax.as_view(),
                    name="comment_revision_ajax",
                ),
                re_path(
                    r"^edit/ajax$",
                    comment.CommentEditAjax.as_view(),
                    name="comment_edit_ajax",
                ),
                re_path(
                    r"^votes/ajax$",
                    comment.CommentVotesAjax.as_view(),
                    name="comment_votes_ajax",
                ),
                re_path(
                    r"^render$",
                    comment.CommentContent.as_view(),
                    name="comment_content",
                ),
            ]
        ),
    ),
    re_path(r"^contests/", paged_list_view(contests.ContestList, "contest_list")),
    re_path(
        r"^contests/summary/(?P<key>\w+)/",
        paged_list_view(contests.ContestsSummaryView, "contests_summary"),
    ),
    re_path(
        r"^contests/official",
        paged_list_view(contests.OfficialContestList, "official_contest_list"),
    ),
    re_path(r"^courses/", paged_list_view(course.CourseList, "course_list")),
    re_path(
        r"^course/(?P<slug>[\w-]*)",
        include(
            [
                re_path(r"^$", course.CourseDetail.as_view(), name="course_detail"),
                re_path(
                    r"^/lesson/(?P<id>\d+)$",
                    course.CourseLessonDetail.as_view(),
                    name="course_lesson_detail",
                ),
                re_path(
                    r"^/lesson/create$",
                    course.CreateCourseLesson.as_view(),
                    name="course_lesson_create",
                ),
                re_path(
                    r"^/edit_lessons$",
                    course.EditCourseLessonsView.as_view(),
                    name="edit_course_lessons",
                ),
                re_path(
                    r"^/edit_lessons_new/(?P<id>\d+)$",
                    course.EditCourseLessonsViewNewWindow.as_view(),
                    name="edit_course_lessons_new",
                ),
                re_path(
                    r"^/grades$",
                    course.CourseStudentResults.as_view(),
                    name="course_grades",
                ),
                re_path(
                    r"^/grades/lesson/(?P<id>\d+)$",
                    course.CourseStudentResultsLesson.as_view(),
                    name="course_grades_lesson",
                ),
                re_path(
                    r"^/add_contest$",
                    course.AddCourseContest.as_view(),
                    name="add_course_contest",
                ),
                re_path(
                    r"^/edit_contest/(?P<contest>\w+)$",
                    course.EditCourseContest.as_view(),
                    name="edit_course_contest",
                ),
                re_path(
                    r"^/contests$",
                    course.CourseContestList.as_view(),
                    name="course_contest_list",
                ),
            ]
        ),
    ),
    re_path(
        r"^contests/(?P<year>\d+)/(?P<month>\d+)/$",
        contests.ContestCalendar.as_view(),
        name="contest_calendar",
    ),
    re_path(
        r"^contests/tag/(?P<name>[a-z-]+)",
        include(
            [
                re_path(r"^$", contests.ContestTagDetail.as_view(), name="contest_tag"),
                re_path(
                    r"^/ajax$",
                    contests.ContestTagDetailAjax.as_view(),
                    name="contest_tag_ajax",
                ),
            ]
        ),
    ),
    re_path(
        r"^contest/(?P<contest>\w+)",
        include(
            [
                re_path(r"^$", contests.ContestDetail.as_view(), name="contest_view"),
                re_path(
                    r"^/moss$", contests.ContestMossView.as_view(), name="contest_moss"
                ),
                re_path(
                    r"^/moss/delete$",
                    contests.ContestMossDelete.as_view(),
                    name="contest_moss_delete",
                ),
                re_path(
                    r"^/clone$", contests.ContestClone.as_view(), name="contest_clone"
                ),
                re_path(
                    r"^/ranking/$",
                    contests.ContestRanking.as_view(),
                    name="contest_ranking",
                ),
                re_path(
                    r"^/final_ranking/$",
                    contests.ContestFinalRanking.as_view(),
                    name="contest_final_ranking",
                ),
                re_path(
                    r"^/join$", contests.ContestJoin.as_view(), name="contest_join"
                ),
                re_path(
                    r"^/leave$", contests.ContestLeave.as_view(), name="contest_leave"
                ),
                re_path(
                    r"^/stats$", contests.ContestStats.as_view(), name="contest_stats"
                ),
                re_path(
                    r"^/submissions/(?P<user>\w+)/(?P<problem>\w+)",
                    paged_list_view(
                        submission.UserContestSubmissions, "contest_user_submissions"
                    ),
                ),
                re_path(
                    r"^/submissions/(?P<participation>\d+)/(?P<problem>\w+)/ajax",
                    paged_list_view(
                        submission.UserContestSubmissionsAjax,
                        "contest_user_submissions_ajax",
                    ),
                ),
                re_path(
                    r"^/submissions",
                    paged_list_view(
                        submission.ContestSubmissions,
                        "contest_submissions",
                    ),
                ),
                re_path(
                    r"^/participations$",
                    contests.ContestParticipationList.as_view(),
                    name="contest_participation_own",
                ),
                re_path(
                    r"^/participations/(?P<user>\w+)$",
                    contests.ContestParticipationList.as_view(),
                    name="contest_participation",
                ),
                re_path(
                    r"^/participation/disqualify$",
                    contests.ContestParticipationDisqualify.as_view(),
                    name="contest_participation_disqualify",
                ),
                re_path(
                    r"^/clarification$",
                    contests.NewContestClarificationView.as_view(),
                    name="new_contest_clarification",
                ),
                re_path(
                    r"^/clarification/ajax$",
                    contests.ContestClarificationAjax.as_view(),
                    name="contest_clarification_ajax",
                ),
                re_path(
                    r"^/$",
                    lambda _, contest: HttpResponsePermanentRedirect(
                        reverse("contest_view", args=[contest])
                    ),
                ),
            ]
        ),
    ),
    re_path(
        r"^organizations/$",
        organization.OrganizationList.as_view(),
        name="organization_list",
    ),
    re_path(
        r"^organizations/add/$",
        organization.AddOrganization.as_view(),
        name="organization_add",
    ),
    re_path(
        r"^organization/(?P<pk>\d+)-(?P<slug>[\w-]*)",
        include(
            [
                re_path(
                    r"^$",
                    organization.OrganizationHome.as_view(),
                    name="organization_home",
                ),
                re_path(
                    r"^/users/",
                    paged_list_view(
                        organization.OrganizationUsers,
                        "organization_users",
                    ),
                ),
                re_path(
                    r"^/problems/",
                    paged_list_view(
                        organization.OrganizationProblems, "organization_problems"
                    ),
                ),
                re_path(
                    r"^/contests/",
                    paged_list_view(
                        organization.OrganizationContests, "organization_contests"
                    ),
                ),
                re_path(
                    r"^/contest/add",
                    organization.AddOrganizationContest.as_view(),
                    name="organization_contest_add",
                ),
                re_path(
                    r"^/contest/edit/(?P<contest>\w+)",
                    organization.EditOrganizationContest.as_view(),
                    name="organization_contest_edit",
                ),
                re_path(
                    r"^/submissions/",
                    paged_list_view(
                        organization.OrganizationSubmissions, "organization_submissions"
                    ),
                ),
                re_path(
                    r"^/join$",
                    organization.JoinOrganization.as_view(),
                    name="join_organization",
                ),
                re_path(
                    r"^/leave$",
                    organization.LeaveOrganization.as_view(),
                    name="leave_organization",
                ),
                re_path(
                    r"^/block$",
                    organization.BlockOrganization.as_view(),
                    name="block_organization",
                ),
                re_path(
                    r"^/unblock$",
                    organization.UnblockOrganization.as_view(),
                    name="unblock_organization",
                ),
                re_path(
                    r"^/edit$",
                    organization.EditOrganization.as_view(),
                    name="edit_organization",
                ),
                re_path(
                    r"^/kick$",
                    organization.KickUserWidgetView.as_view(),
                    name="organization_user_kick",
                ),
                re_path(
                    r"^/add_member$",
                    organization.AddOrganizationMember.as_view(),
                    name="add_organization_member",
                ),
                re_path(
                    r"^/blog/add$",
                    organization.AddOrganizationBlog.as_view(),
                    name="add_organization_blog",
                ),
                re_path(
                    r"^/blog/edit/(?P<blog_pk>\d+)$",
                    organization.EditOrganizationBlog.as_view(),
                    name="edit_organization_blog",
                ),
                re_path(
                    r"^/blog/pending$",
                    organization.PendingBlogs.as_view(),
                    name="organization_pending_blogs",
                ),
                re_path(
                    r"^/request$",
                    organization.RequestJoinOrganization.as_view(),
                    name="request_organization",
                ),
                re_path(
                    r"^/request/(?P<rpk>\d+)$",
                    organization.OrganizationRequestDetail.as_view(),
                    name="request_organization_detail",
                ),
                re_path(
                    r"^/requests/",
                    include(
                        [
                            re_path(
                                r"^pending$",
                                organization.OrganizationRequestView.as_view(),
                                name="organization_requests_pending",
                            ),
                            re_path(
                                r"^log$",
                                organization.OrganizationRequestLog.as_view(),
                                name="organization_requests_log",
                            ),
                            re_path(
                                r"^approved$",
                                organization.OrganizationRequestLog.as_view(
                                    states=("A",), tab="approved"
                                ),
                                name="organization_requests_approved",
                            ),
                            re_path(
                                r"^rejected$",
                                organization.OrganizationRequestLog.as_view(
                                    states=("R",), tab="rejected"
                                ),
                                name="organization_requests_rejected",
                            ),
                        ]
                    ),
                ),
                re_path(
                    r"^/$",
                    lambda _, pk, slug: HttpResponsePermanentRedirect(
                        reverse("organization_home", args=[pk, slug])
                    ),
                ),
            ]
        ),
    ),
    re_path(r"^runtimes/$", language.LanguageList.as_view(), name="runtime_list"),
    re_path(r"^runtimes/matrix/$", status.version_matrix, name="version_matrix"),
    re_path(r"^status/$", status.status_all, name="status_all"),
    re_path(
        r"^api/",
        include(
            [
                re_path(r"^contest/list$", api.api_v1_contest_list),
                re_path(r"^contest/info/(\w+)$", api.api_v1_contest_detail),
                re_path(r"^problem/list$", api.api_v1_problem_list),
                re_path(r"^problem/info/(\w+)$", api.api_v1_problem_info),
                re_path(r"^user/list$", api.api_v1_user_list),
                re_path(r"^user/info/(\w+)$", api.api_v1_user_info),
                re_path(r"^user/submissions/(\w+)$", api.api_v1_user_submissions),
            ]
        ),
    ),
    re_path(r"^blog/", blog.PostList.as_view(), name="blog_post_list"),
    re_path(
        r"^post/(?P<id>\d+)-(?P<slug>.*)$", blog.PostView.as_view(), name="blog_post"
    ),
    re_path(
        r"^license/(?P<key>[-\w.]+)$", license.LicenseDetail.as_view(), name="license"
    ),
    re_path(
        r"^mailgun/mail_activate/$",
        mailgun.MailgunActivationView.as_view(),
        name="mailgun_activate",
    ),
    re_path(
        r"^widgets/",
        include(
            [
                re_path(
                    r"^contest_mode$",
                    contests.update_contest_mode,
                    name="contest_mode_ajax",
                ),
                re_path(
                    r"^rejudge$", widgets.rejudge_submission, name="submission_rejudge"
                ),
                re_path(
                    r"^single_submission$",
                    submission.single_submission_query,
                    name="submission_single_query",
                ),
                re_path(
                    r"^submission_testcases$",
                    submission.SubmissionTestCaseQuery.as_view(),
                    name="submission_testcases_query",
                ),
                re_path(
                    r"^detect_timezone$",
                    widgets.DetectTimezone.as_view(),
                    name="detect_timezone",
                ),
                re_path(r"^status-table$", status.status_table, name="status_table"),
                re_path(
                    r"^template$",
                    problem.LanguageTemplateAjax.as_view(),
                    name="language_template_ajax",
                ),
                re_path(
                    r"^select2/",
                    include(
                        [
                            re_path(
                                r"^user_search$",
                                UserSearchSelect2View.as_view(),
                                name="user_search_select2_ajax",
                            ),
                            re_path(
                                r"^user_search_chat$",
                                ChatUserSearchSelect2View.as_view(),
                                name="chat_user_search_select2_ajax",
                            ),
                            re_path(
                                r"^contest_users/(?P<contest>\w+)$",
                                ContestUserSearchSelect2View.as_view(),
                                name="contest_user_search_select2_ajax",
                            ),
                            re_path(
                                r"^ticket_user$",
                                TicketUserSelect2View.as_view(),
                                name="ticket_user_select2_ajax",
                            ),
                            re_path(
                                r"^ticket_assignee$",
                                AssigneeSelect2View.as_view(),
                                name="ticket_assignee_select2_ajax",
                            ),
                            re_path(
                                r"^problem_authors$",
                                ProblemAuthorSearchSelect2View.as_view(),
                                name="problem_authors_select2_ajax",
                            ),
                        ]
                    ),
                ),
                re_path(
                    r"^preview/",
                    include(
                        [
                            re_path(
                                r"^problem$",
                                preview.ProblemMarkdownPreviewView.as_view(),
                                name="problem_preview",
                            ),
                            re_path(
                                r"^blog$",
                                preview.BlogMarkdownPreviewView.as_view(),
                                name="blog_preview",
                            ),
                            re_path(
                                r"^contest$",
                                preview.ContestMarkdownPreviewView.as_view(),
                                name="contest_preview",
                            ),
                            re_path(
                                r"^comment$",
                                preview.CommentMarkdownPreviewView.as_view(),
                                name="comment_preview",
                            ),
                            re_path(
                                r"^profile$",
                                preview.ProfileMarkdownPreviewView.as_view(),
                                name="profile_preview",
                            ),
                            re_path(
                                r"^organization$",
                                preview.OrganizationMarkdownPreviewView.as_view(),
                                name="organization_preview",
                            ),
                            re_path(
                                r"^solution$",
                                preview.SolutionMarkdownPreviewView.as_view(),
                                name="solution_preview",
                            ),
                            re_path(
                                r"^license$",
                                preview.LicenseMarkdownPreviewView.as_view(),
                                name="license_preview",
                            ),
                            re_path(
                                r"^ticket$",
                                preview.TicketMarkdownPreviewView.as_view(),
                                name="ticket_preview",
                            ),
                        ]
                    ),
                ),
            ]
        ),
    ),
    re_path(
        r"^stats/",
        include(
            [
                re_path(
                    "^language/",
                    include(
                        [
                            re_path(
                                "^$",
                                stats.StatLanguage.as_view(),
                                name="language_stats",
                            ),
                        ]
                    ),
                ),
                re_path(
                    "^site/",
                    include(
                        [
                            re_path("^$", stats.StatSite.as_view(), name="site_stats"),
                        ]
                    ),
                ),
            ]
        ),
    ),
    re_path(
        r"^tickets/",
        include(
            [
                re_path(r"^$", ticket.TicketList.as_view(), name="ticket_list"),
                re_path(
                    r"^ajax$", ticket.TicketListDataAjax.as_view(), name="ticket_ajax"
                ),
            ]
        ),
    ),
    re_path(
        r"^ticket/(?P<pk>\d+)",
        include(
            [
                re_path(r"^$", ticket.TicketView.as_view(), name="ticket"),
                re_path(
                    r"^/ajax$",
                    ticket.TicketMessageDataAjax.as_view(),
                    name="ticket_message_ajax",
                ),
                re_path(
                    r"^/open$",
                    ticket.TicketStatusChangeView.as_view(open=True),
                    name="ticket_open",
                ),
                re_path(
                    r"^/close$",
                    ticket.TicketStatusChangeView.as_view(open=False),
                    name="ticket_close",
                ),
                re_path(
                    r"^/notes$",
                    ticket.TicketNotesEditView.as_view(),
                    name="ticket_notes",
                ),
            ]
        ),
    ),
    re_path(
        r"^sitemap\.xml$",
        sitemap,
        {
            "sitemaps": {
                "problem": ProblemSitemap,
                "user": UserSitemap,
                "home": HomePageSitemap,
                "contest": ContestSitemap,
                "organization": OrganizationSitemap,
                "blog": BlogPostSitemap,
                "solutions": SolutionSitemap,
                "pages": UrlSitemap(
                    [
                        {"location": "/about/", "priority": 0.9},
                    ]
                ),
            }
        },
    ),
    re_path(
        r"^judge-select2/",
        include(
            [
                re_path(
                    r"^profile/$", UserSelect2View.as_view(), name="profile_select2"
                ),
                re_path(
                    r"^organization/$",
                    OrganizationSelect2View.as_view(),
                    name="organization_select2",
                ),
                re_path(
                    r"^problem/$", ProblemSelect2View.as_view(), name="problem_select2"
                ),
                re_path(
                    r"^contest/$", ContestSelect2View.as_view(), name="contest_select2"
                ),
            ]
        ),
    ),
    re_path(
        r"^tasks/",
        include(
            [
                re_path(
                    r"^status/(?P<task_id>[A-Za-z0-9-]*)$",
                    tasks.task_status,
                    name="task_status",
                ),
                re_path(
                    r"^ajax_status$", tasks.task_status_ajax, name="task_status_ajax"
                ),
                re_path(r"^success$", tasks.demo_success),
                re_path(r"^failure$", tasks.demo_failure),
                re_path(r"^progress$", tasks.demo_progress),
            ]
        ),
    ),
    re_path(r"^about/", about.about, name="about"),
    re_path(
        r"^custom_checker_sample/",
        about.custom_checker_sample,
        name="custom_checker_sample",
    ),
    re_path(
        r"^chat/",
        include(
            [
                re_path(
                    r"^(?P<room_id>\d*)$",
                    login_required(chat.ChatView.as_view()),
                    name="chat",
                ),
                re_path(r"^delete/$", chat.delete_message, name="delete_chat_message"),
                re_path(r"^mute/$", chat.mute_message, name="mute_chat_message"),
                re_path(r"^post/$", chat.post_message, name="post_chat_message"),
                re_path(r"^ajax$", chat.chat_message_ajax, name="chat_message_ajax"),
                re_path(
                    r"^online_status/ajax$",
                    chat.online_status_ajax,
                    name="online_status_ajax",
                ),
                re_path(
                    r"^get_or_create_room$",
                    chat.get_or_create_room,
                    name="get_or_create_room",
                ),
                re_path(
                    r"^update_last_seen$",
                    chat.update_last_seen,
                    name="update_last_seen",
                ),
                re_path(
                    r"^online_status/user/ajax$",
                    chat.user_online_status_ajax,
                    name="user_online_status_ajax",
                ),
                re_path(
                    r"^toggle_ignore/(?P<user_id>\d+)$",
                    chat.toggle_ignore,
                    name="toggle_ignore",
                ),
            ]
        ),
    ),
    re_path(
        r"^internal/",
        include(
            [
                re_path(
                    r"^problem$",
                    internal.InternalProblem.as_view(),
                    name="internal_problem",
                ),
                re_path(
                    r"^problem_votes$",
                    internal.get_problem_votes,
                    name="internal_problem_votes",
                ),
                re_path(
                    r"^problem_queue$",
                    internal.InternalProblemQueue.as_view(),
                    name="internal_problem_queue",
                ),
                re_path(
                    r"^problem_queue_mark_private$",
                    internal.mark_problem_private,
                    name="internal_mark_problem_private",
                ),
                re_path(
                    r"^request_time$",
                    internal.InternalRequestTime.as_view(),
                    name="internal_request_time",
                ),
                re_path(
                    r"^request_time_detail$",
                    internal.InternalRequestTimeDetail.as_view(),
                    name="internal_request_time_detail",
                ),
                re_path(
                    r"^internal_slow_request$",
                    internal.InternalSlowRequest.as_view(),
                    name="internal_slow_request",
                ),
                re_path(
                    r"^internal_slow_request_detail$",
                    internal.InternalSlowRequestDetail.as_view(),
                    name="internal_slow_request_detail",
                ),
            ]
        ),
    ),
    re_path(
        r"^notifications/",
        paged_list_view(notification.NotificationList, "notification"),
    ),
    re_path(
        r"^import_users/",
        include(
            [
                re_path(r"^$", user.ImportUsersView.as_view(), name="import_users"),
                re_path(
                    r"post_file/$",
                    user.import_users_post_file,
                    name="import_users_post_file",
                ),
                re_path(
                    r"submit/$", user.import_users_submit, name="import_users_submit"
                ),
                re_path(
                    r"sample/$", user.sample_import_users, name="import_users_sample"
                ),
            ]
        ),
    ),
    re_path(
        r"^volunteer/",
        include(
            [
                re_path(
                    r"^problem/vote$",
                    volunteer.vote_problem,
                    name="volunteer_problem_vote",
                ),
            ]
        ),
    ),
    re_path(
        r"^resolver/(?P<contest>\w+)", resolver.Resolver.as_view(), name="resolver"
    ),
    re_path(r"^upload/$", custom_file_upload.file_upload, name="custom_file_upload"),
] + url_static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if "debug_toolbar.middleware.DebugToolbarMiddleware" in settings.MIDDLEWARE:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))

favicon_paths = [
    "apple-touch-icon-180x180.png",
    "apple-touch-icon-114x114.png",
    "android-chrome-72x72.png",
    "apple-touch-icon-57x57.png",
    "apple-touch-icon-72x72.png",
    "apple-touch-icon.png",
    "mstile-70x70.png",
    "android-chrome-36x36.png",
    "apple-touch-icon-precomposed.png",
    "apple-touch-icon-76x76.png",
    "apple-touch-icon-60x60.png",
    "android-chrome-96x96.png",
    "mstile-144x144.png",
    "mstile-150x150.png",
    "safari-pinned-tab.svg",
    "android-chrome-144x144.png",
    "apple-touch-icon-152x152.png",
    "favicon-96x96.png",
    "favicon-32x32.png",
    "favicon-16x16.png",
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "android-chrome-48x48.png",
    "mstile-310x150.png",
    "apple-touch-icon-144x144.png",
    "browserconfig.xml",
    "manifest.json",
    "apple-touch-icon-120x120.png",
    "mstile-310x310.png",
    "reload.png",
]

for favicon in favicon_paths:
    urlpatterns.append(
        re_path(r"^%s$" % favicon, RedirectView.as_view(url=static("icons/" + favicon)))
    )

handler404 = "judge.views.error.error404"
handler403 = "judge.views.error.error403"
handler500 = "judge.views.error.error500"

if "impersonate" in settings.INSTALLED_APPS:
    urlpatterns.append(re_path(r"^impersonate/", include("impersonate.urls")))
