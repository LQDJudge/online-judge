import chat_box.views as chat

from django.conf import settings
from django.conf.urls import include, url
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.sitemaps.views import sitemap
from django.http import Http404, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.templatetags.static import static
from django.urls import reverse
from django.utils.functional import lazystr
from django.utils.translation import ugettext_lazy as _
from django.views.generic import RedirectView
from django.contrib.auth.decorators import login_required
from django.conf.urls.static import static as url_static


from judge.feed import (
    AtomBlogFeed,
    AtomCommentFeed,
    AtomProblemFeed,
    BlogFeed,
    CommentFeed,
    ProblemFeed,
)
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
    CommentSelect2View,
    ContestSelect2View,
    ContestUserSearchSelect2View,
    OrganizationSelect2View,
    ProblemSelect2View,
    TicketUserSelect2View,
    UserSearchSelect2View,
    UserSelect2View,
)

admin.autodiscover()

register_patterns = [
    url(
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
    url(
        r"^activate/(?P<activation_key>\w+)/$",
        ActivationView.as_view(title=_("Activation key invalid")),
        name="registration_activate",
    ),
    url(
        r"^register/$",
        RegistrationView.as_view(title=_("Register")),
        name="registration_register",
    ),
    url(
        r"^register/complete/$",
        TitledTemplateView.as_view(
            template_name="registration/registration_complete.html",
            title=_("Registration Completed"),
        ),
        name="registration_complete",
    ),
    url(
        r"^register/closed/$",
        TitledTemplateView.as_view(
            template_name="registration/registration_closed.html",
            title=_("Registration not allowed"),
        ),
        name="registration_disallowed",
    ),
    url(
        r"^login/$",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            extra_context={"title": _("Login")},
            authentication_form=CustomAuthenticationForm,
            redirect_authenticated_user=True,
        ),
        name="auth_login",
    ),
    url(r"^logout/$", user.UserLogoutView.as_view(), name="auth_logout"),
    url(
        r"^password/change/$",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change_form.html",
        ),
        name="password_change",
    ),
    url(
        r"^password/change/done/$",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html",
        ),
        name="password_change_done",
    ),
    url(
        r"^password/reset/$",
        auth_views.PasswordResetView.as_view(
            template_name="registration/password_reset.html",
            html_email_template_name="registration/password_reset_email.html",
            email_template_name="registration/password_reset_email.txt",
        ),
        name="password_reset",
    ),
    url(
        r"^password/reset/confirm/(?P<uidb64>[0-9A-Za-z]+)-(?P<token>.+)/$",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
        ),
        name="password_reset_confirm",
    ),
    url(
        r"^password/reset/complete/$",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html",
        ),
        name="password_reset_complete",
    ),
    url(
        r"^password/reset/done/$",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html",
        ),
        name="password_reset_done",
    ),
    url(r"^email/change/$", email.email_change_view, name="email_change"),
    url(
        r"^email/change/verify/(?P<uidb64>[0-9A-Za-z]+)-(?P<token>.+)/$",
        email.verify_email_view,
        name="email_change_verify",
    ),
    url(
        r"^email/change/pending$",
        email.email_change_pending_view,
        name="email_change_pending",
    ),
    url(r"^social/error/$", register.social_auth_error, name="social_auth_error"),
    url(r"^2fa/$", totp.TOTPLoginView.as_view(), name="login_2fa"),
    url(r"^2fa/enable/$", totp.TOTPEnableView.as_view(), name="enable_2fa"),
    url(r"^2fa/disable/$", totp.TOTPDisableView.as_view(), name="disable_2fa"),
]


def exception(request):
    if not request.user.is_superuser:
        raise Http404()
    raise RuntimeError("@Xyene asked me to cause this")


def paged_list_view(view, name, **kwargs):
    return include(
        [
            url(r"^$", view.as_view(**kwargs), name=name),
            url(r"^(?P<page>\d+)$", view.as_view(**kwargs), name=name),
        ]
    )


urlpatterns = [
    url("", include("pagedown.urls")),
    url(
        r"^$",
        blog.PostList.as_view(template_name="home.html", title=_("Home")),
        kwargs={"page": 1},
        name="home",
    ),
    url(r"^500/$", exception),
    url(r"^toggle_darkmode/$", user.toggle_darkmode, name="toggle_darkmode"),
    url(r"^admin/", admin.site.urls),
    url(r"^i18n/", include("django.conf.urls.i18n")),
    url(r"^accounts/", include(register_patterns)),
    url(r"^", include("social_django.urls")),
    url(
        r"^feed/",
        include(
            [
                url(r"^tickets/$", blog.TicketFeed.as_view(), name="ticket_feed"),
                url(r"^comments/$", blog.CommentFeed.as_view(), name="comment_feed"),
            ]
        ),
    ),
    url(r"^problems/", paged_list_view(problem.ProblemList, "problem_list")),
    url(r"^problems/random/$", problem.RandomProblem.as_view(), name="problem_random"),
    url(
        r"^problems/feed/$",
        problem.ProblemFeed.as_view(feed_type="for_you"),
        name="problem_feed",
    ),
    url(
        r"^problems/feed/new/$",
        problem.ProblemFeed.as_view(feed_type="new"),
        name="problem_feed_new",
    ),
    url(
        r"^problems/feed/volunteer/$",
        problem.ProblemFeed.as_view(feed_type="volunteer"),
        name="problem_feed_volunteer",
    ),
    url(
        r"^problem/(?P<problem>[^/]+)",
        include(
            [
                url(r"^$", problem.ProblemDetail.as_view(), name="problem_detail"),
                url(
                    r"^/editorial$",
                    problem.ProblemSolution.as_view(),
                    name="problem_editorial",
                ),
                url(r"^/raw$", problem.ProblemRaw.as_view(), name="problem_raw"),
                url(r"^/pdf$", problem.ProblemPdfView.as_view(), name="problem_pdf"),
                url(
                    r"^/pdf/(?P<language>[a-z-]+)$",
                    problem.ProblemPdfView.as_view(),
                    name="problem_pdf",
                ),
                url(
                    r"^/pdf_description$",
                    problem.ProblemPdfDescriptionView.as_view(),
                    name="problem_pdf_description",
                ),
                url(r"^/clone", problem.ProblemClone.as_view(), name="problem_clone"),
                url(r"^/submit$", problem.problem_submit, name="problem_submit"),
                url(
                    r"^/resubmit/(?P<submission>\d+)$",
                    problem.problem_submit,
                    name="problem_submit",
                ),
                url(
                    r"^/rank/",
                    paged_list_view(
                        ranked_submission.RankedSubmissions, "ranked_submissions"
                    ),
                ),
                url(
                    r"^/submissions/",
                    paged_list_view(
                        submission.ProblemSubmissions, "chronological_submissions"
                    ),
                ),
                url(
                    r"^/submissions/(?P<user>\w+)/",
                    paged_list_view(
                        submission.UserProblemSubmissions, "user_submissions"
                    ),
                ),
                url(
                    r"^/$",
                    lambda _, problem: HttpResponsePermanentRedirect(
                        reverse("problem_detail", args=[problem])
                    ),
                ),
                url(r"^/test_data$", ProblemDataView.as_view(), name="problem_data"),
                url(r"^/test_data/init$", problem_init_view, name="problem_data_init"),
                url(
                    r"^/test_data/diff$",
                    ProblemSubmissionDiff.as_view(),
                    name="problem_submission_diff",
                ),
                url(
                    r"^/test_data/upload$",
                    ProblemZipUploadView.as_view(),
                    name="problem_zip_upload",
                ),
                url(
                    r"^/data/(?P<path>.+)$", problem_data_file, name="problem_data_file"
                ),
                url(
                    r"^/tickets$",
                    ticket.ProblemTicketListView.as_view(),
                    name="problem_ticket_list",
                ),
                url(
                    r"^/tickets/new$",
                    ticket.NewProblemTicketView.as_view(),
                    name="new_problem_ticket",
                ),
                url(
                    r"^/manage/submission",
                    include(
                        [
                            url(
                                "^$",
                                problem_manage.ManageProblemSubmissionView.as_view(),
                                name="problem_manage_submissions",
                            ),
                            url(
                                "^/action$",
                                problem_manage.ActionSubmissionsView.as_view(),
                                name="problem_submissions_action",
                            ),
                            url(
                                "^/action/preview$",
                                problem_manage.PreviewActionSubmissionsView.as_view(),
                                name="problem_submissions_rejudge_preview",
                            ),
                            url(
                                "^/rejudge/success/(?P<task_id>[A-Za-z0-9-]*)$",
                                problem_manage.rejudge_success,
                                name="problem_submissions_rejudge_success",
                            ),
                            url(
                                "^/rescore/all$",
                                problem_manage.RescoreAllSubmissionsView.as_view(),
                                name="problem_submissions_rescore_all",
                            ),
                            url(
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
    url(
        r"^submissions/", paged_list_view(submission.AllSubmissions, "all_submissions")
    ),
    url(
        r"^submissions/user/(?P<user>\w+)/",
        paged_list_view(submission.AllUserSubmissions, "all_user_submissions"),
    ),
    url(
        r"^submissions/friends/",
        paged_list_view(submission.AllFriendSubmissions, "all_friend_submissions"),
    ),
    url(
        r"^src/(?P<submission>\d+)/raw$",
        submission.SubmissionSourceRaw.as_view(),
        name="submission_source_raw",
    ),
    url(
        r"^submission/(?P<submission>\d+)",
        include(
            [
                url(
                    r"^$",
                    submission.SubmissionStatus.as_view(),
                    name="submission_status",
                ),
                url(r"^/abort$", submission.abort_submission, name="submission_abort"),
                url(r"^/html$", submission.single_submission),
            ]
        ),
    ),
    url(
        r"^markdown_editor/",
        markdown_editor.MarkdownEditor.as_view(),
        name="markdown_editor",
    ),
    url(
        r"^submission_source_file/(?P<filename>(\w|\.)+)",
        submission.SubmissionSourceFileView.as_view(),
        name="submission_source_file",
    ),
    url(
        r"^users/",
        include(
            [
                url(r"^$", user.users, name="user_list"),
                url(
                    r"^(?P<page>\d+)$",
                    lambda request, page: HttpResponsePermanentRedirect(
                        "%s?page=%s" % (reverse("user_list"), page)
                    ),
                ),
                url(
                    r"^find$", user.user_ranking_redirect, name="user_ranking_redirect"
                ),
            ]
        ),
    ),
    url(r"^user$", user.UserAboutPage.as_view(), name="user_page"),
    url(r"^edit/profile/$", user.edit_profile, name="user_edit_profile"),
    url(r"^user/bookmarks", user.UserBookMarkPage.as_view(), name="user_bookmark"),
    url(
        r"^user/(?P<user>\w+)",
        include(
            [
                url(r"^$", user.UserAboutPage.as_view(), name="user_page"),
                url(
                    r"^/solved",
                    include(
                        [
                            url(
                                r"^$",
                                user.UserProblemsPage.as_view(),
                                name="user_problems",
                            ),
                            url(
                                r"/ajax$",
                                user.UserPerformancePointsAjax.as_view(),
                                name="user_pp_ajax",
                            ),
                        ]
                    ),
                ),
                url(
                    r"^/submissions/",
                    paged_list_view(
                        submission.AllUserSubmissions, "all_user_submissions_old"
                    ),
                ),
                url(
                    r"^/submissions/",
                    lambda _, user: HttpResponsePermanentRedirect(
                        reverse("all_user_submissions", args=[user])
                    ),
                ),
                url(
                    r"^/$",
                    lambda _, user: HttpResponsePermanentRedirect(
                        reverse("user_page", args=[user])
                    ),
                ),
            ]
        ),
    ),
    url(r"^pagevotes/upvote/$", pagevote.upvote_page, name="pagevote_upvote"),
    url(r"^pagevotes/downvote/$", pagevote.downvote_page, name="pagevote_downvote"),
    url(r"^bookmarks/dobookmark/$", bookmark.dobookmark_page, name="dobookmark"),
    url(r"^bookmarks/undobookmark/$", bookmark.undobookmark_page, name="undobookmark"),
    url(r"^comments/upvote/$", comment.upvote_comment, name="comment_upvote"),
    url(r"^comments/downvote/$", comment.downvote_comment, name="comment_downvote"),
    url(r"^comments/hide/$", comment.comment_hide, name="comment_hide"),
    url(r"^comments/get_replies/$", comment.get_replies, name="comment_get_replies"),
    url(r"^comments/show_more/$", comment.get_show_more, name="comment_show_more"),
    url(
        r"^comments/(?P<id>\d+)/",
        include(
            [
                url(r"^edit$", comment.CommentEdit.as_view(), name="comment_edit"),
                url(
                    r"^history/ajax$",
                    comment.CommentRevisionAjax.as_view(),
                    name="comment_revision_ajax",
                ),
                url(
                    r"^edit/ajax$",
                    comment.CommentEditAjax.as_view(),
                    name="comment_edit_ajax",
                ),
                url(
                    r"^votes/ajax$",
                    comment.CommentVotesAjax.as_view(),
                    name="comment_votes_ajax",
                ),
                url(
                    r"^render$",
                    comment.CommentContent.as_view(),
                    name="comment_content",
                ),
            ]
        ),
    ),
    url(r"^contests/", paged_list_view(contests.ContestList, "contest_list")),
    url(
        r"^contests/summary/(?P<key>\w+)$",
        contests.contests_summary_view,
        name="contests_summary",
    ),
    url(r"^course/", paged_list_view(course.CourseList, "course_list")),
    url(
        r"^contests/(?P<year>\d+)/(?P<month>\d+)/$",
        contests.ContestCalendar.as_view(),
        name="contest_calendar",
    ),
    url(
        r"^contests/tag/(?P<name>[a-z-]+)",
        include(
            [
                url(r"^$", contests.ContestTagDetail.as_view(), name="contest_tag"),
                url(
                    r"^/ajax$",
                    contests.ContestTagDetailAjax.as_view(),
                    name="contest_tag_ajax",
                ),
            ]
        ),
    ),
    url(
        r"^contest/(?P<contest>\w+)",
        include(
            [
                url(r"^$", contests.ContestDetail.as_view(), name="contest_view"),
                url(
                    r"^/moss$", contests.ContestMossView.as_view(), name="contest_moss"
                ),
                url(
                    r"^/moss/delete$",
                    contests.ContestMossDelete.as_view(),
                    name="contest_moss_delete",
                ),
                url(r"^/clone$", contests.ContestClone.as_view(), name="contest_clone"),
                url(
                    r"^/ranking/$",
                    contests.ContestRanking.as_view(),
                    name="contest_ranking",
                ),
                url(
                    r"^/final_ranking/$",
                    contests.ContestFinalRanking.as_view(),
                    name="contest_final_ranking",
                ),
                url(
                    r"^/ranking/ajax$",
                    contests.contest_ranking_ajax,
                    name="contest_ranking_ajax",
                ),
                url(r"^/join$", contests.ContestJoin.as_view(), name="contest_join"),
                url(r"^/leave$", contests.ContestLeave.as_view(), name="contest_leave"),
                url(r"^/stats$", contests.ContestStats.as_view(), name="contest_stats"),
                url(
                    r"^/submissions/(?P<user>\w+)/(?P<problem>\w+)",
                    paged_list_view(
                        submission.UserContestSubmissions, "contest_user_submissions"
                    ),
                ),
                url(
                    r"^/submissions/(?P<participation>\d+)/(?P<problem>\w+)/ajax",
                    paged_list_view(
                        submission.UserContestSubmissionsAjax,
                        "contest_user_submissions_ajax",
                    ),
                ),
                url(
                    r"^/participations$",
                    contests.ContestParticipationList.as_view(),
                    name="contest_participation_own",
                ),
                url(
                    r"^/participations/(?P<user>\w+)$",
                    contests.ContestParticipationList.as_view(),
                    name="contest_participation",
                ),
                url(
                    r"^/participation/disqualify$",
                    contests.ContestParticipationDisqualify.as_view(),
                    name="contest_participation_disqualify",
                ),
                url(
                    r"^/clarification$",
                    contests.NewContestClarificationView.as_view(),
                    name="new_contest_clarification",
                ),
                url(
                    r"^/clarification/ajax$",
                    contests.ContestClarificationAjax.as_view(),
                    name="contest_clarification_ajax",
                ),
                url(
                    r"^/$",
                    lambda _, contest: HttpResponsePermanentRedirect(
                        reverse("contest_view", args=[contest])
                    ),
                ),
            ]
        ),
    ),
    url(
        r"^organizations/$",
        organization.OrganizationList.as_view(),
        name="organization_list",
    ),
    url(
        r"^organizations/add/$",
        organization.AddOrganization.as_view(),
        name="organization_add",
    ),
    url(
        r"^organization/(?P<pk>\d+)-(?P<slug>[\w-]*)",
        include(
            [
                url(
                    r"^$",
                    organization.OrganizationHome.as_view(),
                    name="organization_home",
                ),
                url(
                    r"^/users/",
                    paged_list_view(
                        organization.OrganizationUsers,
                        "organization_users",
                    ),
                ),
                url(
                    r"^/problems/",
                    paged_list_view(
                        organization.OrganizationProblems, "organization_problems"
                    ),
                ),
                url(
                    r"^/contests/",
                    paged_list_view(
                        organization.OrganizationContests, "organization_contests"
                    ),
                ),
                url(
                    r"^/contest/add",
                    organization.AddOrganizationContest.as_view(),
                    name="organization_contest_add",
                ),
                url(
                    r"^/contest/edit/(?P<contest>\w+)",
                    organization.EditOrganizationContest.as_view(),
                    name="organization_contest_edit",
                ),
                url(
                    r"^/submissions/",
                    paged_list_view(
                        organization.OrganizationSubmissions, "organization_submissions"
                    ),
                ),
                url(
                    r"^/join$",
                    organization.JoinOrganization.as_view(),
                    name="join_organization",
                ),
                url(
                    r"^/leave$",
                    organization.LeaveOrganization.as_view(),
                    name="leave_organization",
                ),
                url(
                    r"^/edit$",
                    organization.EditOrganization.as_view(),
                    name="edit_organization",
                ),
                url(
                    r"^/kick$",
                    organization.KickUserWidgetView.as_view(),
                    name="organization_user_kick",
                ),
                url(
                    r"^/add_member$",
                    organization.AddOrganizationMember.as_view(),
                    name="add_organization_member",
                ),
                url(
                    r"^/blog/add$",
                    organization.AddOrganizationBlog.as_view(),
                    name="add_organization_blog",
                ),
                url(
                    r"^/blog/edit/(?P<blog_pk>\d+)$",
                    organization.EditOrganizationBlog.as_view(),
                    name="edit_organization_blog",
                ),
                url(
                    r"^/blog/pending$",
                    organization.PendingBlogs.as_view(),
                    name="organization_pending_blogs",
                ),
                url(
                    r"^/request$",
                    organization.RequestJoinOrganization.as_view(),
                    name="request_organization",
                ),
                url(
                    r"^/request/(?P<rpk>\d+)$",
                    organization.OrganizationRequestDetail.as_view(),
                    name="request_organization_detail",
                ),
                url(
                    r"^/requests/",
                    include(
                        [
                            url(
                                r"^pending$",
                                organization.OrganizationRequestView.as_view(),
                                name="organization_requests_pending",
                            ),
                            url(
                                r"^log$",
                                organization.OrganizationRequestLog.as_view(),
                                name="organization_requests_log",
                            ),
                            url(
                                r"^approved$",
                                organization.OrganizationRequestLog.as_view(
                                    states=("A",), tab="approved"
                                ),
                                name="organization_requests_approved",
                            ),
                            url(
                                r"^rejected$",
                                organization.OrganizationRequestLog.as_view(
                                    states=("R",), tab="rejected"
                                ),
                                name="organization_requests_rejected",
                            ),
                        ]
                    ),
                ),
                url(
                    r"^/$",
                    lambda _, pk, slug: HttpResponsePermanentRedirect(
                        reverse("organization_home", args=[pk, slug])
                    ),
                ),
            ]
        ),
    ),
    url(r"^runtimes/$", language.LanguageList.as_view(), name="runtime_list"),
    url(r"^runtimes/matrix/$", status.version_matrix, name="version_matrix"),
    url(r"^status/$", status.status_all, name="status_all"),
    url(
        r"^api/",
        include(
            [
                url(r"^contest/list$", api.api_v1_contest_list),
                url(r"^contest/info/(\w+)$", api.api_v1_contest_detail),
                url(r"^problem/list$", api.api_v1_problem_list),
                url(r"^problem/info/(\w+)$", api.api_v1_problem_info),
                url(r"^user/list$", api.api_v1_user_list),
                url(r"^user/info/(\w+)$", api.api_v1_user_info),
                url(r"^user/submissions/(\w+)$", api.api_v1_user_submissions),
            ]
        ),
    ),
    url(r"^blog/", blog.PostList.as_view(), name="blog_post_list"),
    url(r"^post/(?P<id>\d+)-(?P<slug>.*)$", blog.PostView.as_view(), name="blog_post"),
    url(r"^license/(?P<key>[-\w.]+)$", license.LicenseDetail.as_view(), name="license"),
    url(
        r"^mailgun/mail_activate/$",
        mailgun.MailgunActivationView.as_view(),
        name="mailgun_activate",
    ),
    url(
        r"^widgets/",
        include(
            [
                url(
                    r"^contest_mode$",
                    contests.update_contest_mode,
                    name="contest_mode_ajax",
                ),
                url(
                    r"^rejudge$", widgets.rejudge_submission, name="submission_rejudge"
                ),
                url(
                    r"^single_submission$",
                    submission.single_submission_query,
                    name="submission_single_query",
                ),
                url(
                    r"^submission_testcases$",
                    submission.SubmissionTestCaseQuery.as_view(),
                    name="submission_testcases_query",
                ),
                url(
                    r"^detect_timezone$",
                    widgets.DetectTimezone.as_view(),
                    name="detect_timezone",
                ),
                url(r"^status-table$", status.status_table, name="status_table"),
                url(
                    r"^template$",
                    problem.LanguageTemplateAjax.as_view(),
                    name="language_template_ajax",
                ),
                url(
                    r"^select2/",
                    include(
                        [
                            url(
                                r"^user_search$",
                                UserSearchSelect2View.as_view(),
                                name="user_search_select2_ajax",
                            ),
                            url(
                                r"^user_search_chat$",
                                ChatUserSearchSelect2View.as_view(),
                                name="chat_user_search_select2_ajax",
                            ),
                            url(
                                r"^contest_users/(?P<contest>\w+)$",
                                ContestUserSearchSelect2View.as_view(),
                                name="contest_user_search_select2_ajax",
                            ),
                            url(
                                r"^ticket_user$",
                                TicketUserSelect2View.as_view(),
                                name="ticket_user_select2_ajax",
                            ),
                            url(
                                r"^ticket_assignee$",
                                AssigneeSelect2View.as_view(),
                                name="ticket_assignee_select2_ajax",
                            ),
                        ]
                    ),
                ),
                url(
                    r"^preview/",
                    include(
                        [
                            url(
                                r"^problem$",
                                preview.ProblemMarkdownPreviewView.as_view(),
                                name="problem_preview",
                            ),
                            url(
                                r"^blog$",
                                preview.BlogMarkdownPreviewView.as_view(),
                                name="blog_preview",
                            ),
                            url(
                                r"^contest$",
                                preview.ContestMarkdownPreviewView.as_view(),
                                name="contest_preview",
                            ),
                            url(
                                r"^comment$",
                                preview.CommentMarkdownPreviewView.as_view(),
                                name="comment_preview",
                            ),
                            url(
                                r"^profile$",
                                preview.ProfileMarkdownPreviewView.as_view(),
                                name="profile_preview",
                            ),
                            url(
                                r"^organization$",
                                preview.OrganizationMarkdownPreviewView.as_view(),
                                name="organization_preview",
                            ),
                            url(
                                r"^solution$",
                                preview.SolutionMarkdownPreviewView.as_view(),
                                name="solution_preview",
                            ),
                            url(
                                r"^license$",
                                preview.LicenseMarkdownPreviewView.as_view(),
                                name="license_preview",
                            ),
                            url(
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
    url(
        r"^feed/",
        include(
            [
                url(r"^problems/rss/$", ProblemFeed(), name="problem_rss"),
                url(r"^problems/atom/$", AtomProblemFeed(), name="problem_atom"),
                url(r"^comment/rss/$", CommentFeed(), name="comment_rss"),
                url(r"^comment/atom/$", AtomCommentFeed(), name="comment_atom"),
                url(r"^blog/rss/$", BlogFeed(), name="blog_rss"),
                url(r"^blog/atom/$", AtomBlogFeed(), name="blog_atom"),
            ]
        ),
    ),
    url(
        r"^stats/",
        include(
            [
                url(
                    "^language/",
                    include(
                        [
                            url(
                                "^$",
                                stats.StatLanguage.as_view(),
                                name="language_stats",
                            ),
                        ]
                    ),
                ),
                url(
                    "^site/",
                    include(
                        [
                            url("^$", stats.StatSite.as_view(), name="site_stats"),
                        ]
                    ),
                ),
            ]
        ),
    ),
    url(
        r"^tickets/",
        include(
            [
                url(r"^$", ticket.TicketList.as_view(), name="ticket_list"),
                url(r"^ajax$", ticket.TicketListDataAjax.as_view(), name="ticket_ajax"),
            ]
        ),
    ),
    url(
        r"^ticket/(?P<pk>\d+)",
        include(
            [
                url(r"^$", ticket.TicketView.as_view(), name="ticket"),
                url(
                    r"^/ajax$",
                    ticket.TicketMessageDataAjax.as_view(),
                    name="ticket_message_ajax",
                ),
                url(
                    r"^/open$",
                    ticket.TicketStatusChangeView.as_view(open=True),
                    name="ticket_open",
                ),
                url(
                    r"^/close$",
                    ticket.TicketStatusChangeView.as_view(open=False),
                    name="ticket_close",
                ),
                url(
                    r"^/notes$",
                    ticket.TicketNotesEditView.as_view(),
                    name="ticket_notes",
                ),
            ]
        ),
    ),
    url(
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
    url(
        r"^judge-select2/",
        include(
            [
                url(r"^profile/$", UserSelect2View.as_view(), name="profile_select2"),
                url(
                    r"^organization/$",
                    OrganizationSelect2View.as_view(),
                    name="organization_select2",
                ),
                url(
                    r"^problem/$", ProblemSelect2View.as_view(), name="problem_select2"
                ),
                url(
                    r"^contest/$", ContestSelect2View.as_view(), name="contest_select2"
                ),
                url(
                    r"^comment/$", CommentSelect2View.as_view(), name="comment_select2"
                ),
            ]
        ),
    ),
    url(
        r"^tasks/",
        include(
            [
                url(
                    r"^status/(?P<task_id>[A-Za-z0-9-]*)$",
                    tasks.task_status,
                    name="task_status",
                ),
                url(r"^ajax_status$", tasks.task_status_ajax, name="task_status_ajax"),
                url(r"^success$", tasks.demo_success),
                url(r"^failure$", tasks.demo_failure),
                url(r"^progress$", tasks.demo_progress),
            ]
        ),
    ),
    url(r"^about/", about.about, name="about"),
    url(
        r"^custom_checker_sample/",
        about.custom_checker_sample,
        name="custom_checker_sample",
    ),
    url(
        r"^chat/",
        include(
            [
                url(
                    r"^(?P<room_id>\d*)$",
                    login_required(chat.ChatView.as_view()),
                    name="chat",
                ),
                url(r"^delete/$", chat.delete_message, name="delete_chat_message"),
                url(r"^mute/$", chat.mute_message, name="mute_chat_message"),
                url(r"^post/$", chat.post_message, name="post_chat_message"),
                url(r"^ajax$", chat.chat_message_ajax, name="chat_message_ajax"),
                url(
                    r"^online_status/ajax$",
                    chat.online_status_ajax,
                    name="online_status_ajax",
                ),
                url(
                    r"^get_or_create_room$",
                    chat.get_or_create_room,
                    name="get_or_create_room",
                ),
                url(
                    r"^update_last_seen$",
                    chat.update_last_seen,
                    name="update_last_seen",
                ),
                url(
                    r"^online_status/user/ajax$",
                    chat.user_online_status_ajax,
                    name="user_online_status_ajax",
                ),
                url(
                    r"^toggle_ignore/(?P<user_id>\d+)$",
                    chat.toggle_ignore,
                    name="toggle_ignore",
                ),
            ]
        ),
    ),
    url(
        r"^internal/",
        include(
            [
                url(
                    r"^problem$",
                    internal.InternalProblem.as_view(),
                    name="internal_problem",
                ),
                url(
                    r"^request_time$",
                    internal.InternalRequestTime.as_view(),
                    name="internal_request_time",
                ),
                url(
                    r"^request_time_detail$",
                    internal.InternalRequestTimeDetail.as_view(),
                    name="internal_request_time_detail",
                ),
                url(
                    r"^internal_slow_request$",
                    internal.InternalSlowRequest.as_view(),
                    name="internal_slow_request",
                ),
                url(
                    r"^internal_slow_request_detail$",
                    internal.InternalSlowRequestDetail.as_view(),
                    name="internal_slow_request_detail",
                ),
            ]
        ),
    ),
    url(
        r"^notifications/",
        login_required(notification.NotificationList.as_view()),
        name="notification",
    ),
    url(
        r"^import_users/",
        include(
            [
                url(r"^$", user.ImportUsersView.as_view(), name="import_users"),
                url(
                    r"post_file/$",
                    user.import_users_post_file,
                    name="import_users_post_file",
                ),
                url(r"submit/$", user.import_users_submit, name="import_users_submit"),
                url(r"sample/$", user.sample_import_users, name="import_users_sample"),
            ]
        ),
    ),
    url(
        r"^volunteer/",
        include(
            [
                url(
                    r"^problem/vote$",
                    volunteer.vote_problem,
                    name="volunteer_problem_vote",
                ),
            ]
        ),
    ),
    url(r"^resolver/(?P<contest>\w+)", resolver.Resolver.as_view(), name="resolver"),
] + url_static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# if hasattr(settings, "INTERNAL_IPS"):
#     urlpatterns.append(url("__debug__/", include("debug_toolbar.urls")))

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
        url(r"^%s$" % favicon, RedirectView.as_view(url=static("icons/" + favicon)))
    )

handler404 = "judge.views.error.error404"
handler403 = "judge.views.error.error403"
handler500 = "judge.views.error.error500"

if "impersonate" in settings.INSTALLED_APPS:
    urlpatterns.append(url(r"^impersonate/", include("impersonate.urls")))
