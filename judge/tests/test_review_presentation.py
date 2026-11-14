from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils.translation import gettext, override

from bs4 import BeautifulSoup

from judge.jinja2.markdown import review_inline_markdown, review_markdown
from judge.markdown import markdown
from judge.ml import semantic_search
from judge.models import ContestReviewRun, ProblemReviewRun, ReviewStage
from judge.auto_review.presentation import (
    review_author_feedback,
    review_list_indentation,
    review_prose,
    review_section_groups,
    review_similarity,
    review_stage_badge,
    review_stage_notes,
)


class ReviewInlineMarkdownTests(SimpleTestCase):
    def test_contest_review_vietnamese_uses_consistent_natural_labels(self):
        labels = {
            "Contest reviews": "Đánh giá kỳ thi",
            "Contest auto-review is currently disabled.": (
                "Tính năng đánh giá tự động kỳ thi hiện đang tắt."
            ),
            "No contest reviews match your filters.": (
                "Không có kết quả đánh giá kỳ thi nào phù hợp với bộ lọc."
            ),
            "Difficulty and diversity": "Độ khó và sự đa dạng của bộ đề",
            "Rules and consistency": "Quy định và tính nhất quán",
            "Select problems for a new review": "Chọn bài để chạy đánh giá mới",
            "Current report": "Kết quả hiện tại",
            "Bound problem reviews": "Kết quả từng bài được dùng trong lượt này",
            "Review used": "Đánh giá đã dùng",
            "Latest review": "Đánh giá mới nhất",
            "Not used": "Chưa dùng",
            "Wait for the review to finish.": "Hãy chờ đánh giá hoàn tất.",
            "Fix the issues identified in this review, then run it again.": (
                "Hãy sửa các vấn đề được nêu trong đánh giá, rồi chạy đánh giá lại."
            ),
            "Request uses an earlier review": "Yêu cầu đang dùng lượt đánh giá trước",
            "This contest assessment still relies on the earlier problem review.": (
                "Kết quả đánh giá kỳ thi này vẫn dựa trên lượt đánh giá trước của bài."
            ),
            "All checks passed.": "Tất cả kiểm tra đều đạt.",
            "No checks ran.": "Chưa có kiểm tra nào được chạy.",
        }
        with override("vi"):
            for source, expected in labels.items():
                with self.subTest(source=source):
                    self.assertEqual(gettext(source), expected)
            self.assertEqual(
                gettext("Please wait %(mmss)s before requesting review again.")
                % {"mmss": "02:30"},
                "Vui lòng chờ 02:30 trước khi chạy đánh giá tiếp.",
            )
            self.assertEqual(
                gettext("Cancelling will use %(count)s author review credits.")
                % {"count": 2},
                "Hủy sẽ trừ tổng cộng 2 lượt đánh giá của tác giả.",
            )

    def test_vietnamese_review_labels_are_clear_and_keep_placeholders(self):
        with override("vi"):
            self.assertEqual(gettext("Readiness"), "Kết quả đánh giá")
            self.assertEqual(gettext("AI coordinator"), "Đánh giá bằng AI")
            self.assertEqual(gettext("Review run"), "Lượt đánh giá")
            self.assertEqual(gettext("Run"), "Lần chạy")
            self.assertEqual(gettext("Similar problems"), "Các bài tương tự")
            self.assertEqual(gettext("Current content"), "Trạng thái nội dung")
            self.assertEqual(gettext("Review needs recovery"), "Đánh giá bị gián đoạn")
            self.assertEqual(gettext("Manual review"), "Cần quản trị viên xem xét")
            self.assertNotEqual(gettext("Manual review"), gettext("Needs author fix"))
            self.assertNotEqual(gettext("Incomplete"), gettext("Fail"))
            self.assertEqual(
                gettext("Author credits: %(count)s/3") % {"count": 2},
                "Tác giả còn 2/3 lượt đánh giá",
            )
            self.assertEqual(
                gettext("Unavailable upstream evidence: %(sections)s")
                % {"sections": "Validator"},
                "Chưa có kết quả từ các phần: Validator",
            )

    def test_reader_feedback_is_separate_from_preserved_technical_summary(self):
        output = {
            "summary": "Validated artifact:testcase_validator; job:internal-id",
            "author_feedback": "Các test đã kiểm tra đều hợp lệ.",
        }
        self.assertEqual(review_author_feedback(output), output["author_feedback"])
        self.assertIn("job:internal-id", output["summary"])
        for feedback in (None, "", "  ", {}, []):
            self.assertEqual(
                review_author_feedback({**output, "author_feedback": feedback}),
                output["summary"],
            )
        self.assertEqual(review_author_feedback({"summary": None}), "")

    def test_title_supports_emphasis_code_and_math_without_block_elements(self):
        rendered = review_inline_markdown(
            r"**Check bounds** for `int`: $x < y$ and $N \le 200\,000$."
        )
        self.assertIn("<strong>Check bounds</strong>", rendered)
        self.assertIn("<code>int</code>", rendered)
        self.assertIn(r'<span class="arithmatex">\(x &lt; y\)</span>', rendered)
        self.assertIn(r'<span class="arithmatex">\(N \le 200\,000\)</span>', rendered)
        self.assertNotIn("<div", rendered)
        self.assertNotIn("<p>", rendered)

    def test_title_sanitizes_html_and_removes_block_and_embedded_elements(self):
        rendered = review_inline_markdown(
            "# **Bounds**\n\n<script>bad()</script>\n\n"
            '<img src="x" onerror="bad()"><iframe src="https://example.org"></iframe>'
            "[unsafe](javascript:bad)"
        )
        self.assertIn("<strong>Bounds</strong>", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)
        for unsafe in ("<script", "<img", "<iframe", "onerror", "javascript:", "<h1"):
            self.assertNotIn(unsafe, rendered)


@override_settings(LANGUAGE_CODE="en")
class ReviewSectionGroupsTests(SimpleTestCase):
    def stage(self, key, output=None, status="D"):
        return ReviewStage(stage_id=key, status=status, output_json=output or {})

    def test_only_applicable_groups_are_shown(self):
        self.assertEqual(review_section_groups([]), [])
        groups = review_section_groups([self.stage("statement_review")])
        self.assertEqual([g["title"] for g in groups], ["Statement and editorial"])

    def test_required_supporting_failure_cannot_hide_behind_primary(self):
        groups = review_section_groups(
            [self.stage("problem_contract", status="E"), self.stage("statement_review")]
        )
        self.assertEqual(groups[0]["status"], "Error")

    def test_optional_unavailable_extraction_does_not_override_test_assessment(self):
        groups = review_section_groups(
            [
                self.stage("extract_stats", {"availability": "unavailable"}),
                self.stage("test_review", {"assessment": "pass"}),
            ]
        )
        self.assertEqual(groups[0]["status"], "Pass")

    def test_final_failure_has_visible_recovery_section(self):
        groups = review_section_groups([self.stage("final_assessment", status="E")])
        self.assertEqual(groups[0]["id"], "final_assessment")
        self.assertEqual(groups[0]["status"], "Error")
        self.assertEqual(review_section_groups([self.stage("final_assessment")]), [])

    def test_similarity_shows_comparison_and_relationship(self):
        retrieval = self.stage(
            "similarity_search",
            {"candidates": [{"ref": "candidate:1", "problem_id": 1}]},
        )
        retrieval.ui_candidates = [
            {"ref": "candidate:1", "url": "/problem/example", "name": "Example"}
        ]
        for relation in (
            "essentially_equivalent",
            "meaningful_variation",
            "shared_technique_only",
            "unrelated",
            "uncertain",
        ):
            output = {
                "evidence": {
                    "comparisons": [
                        {
                            "candidate_ref": "candidate:1",
                            "relationship": relation,
                            "confidence": "high",
                            "core_comparison": "Compare $n$.",
                            "substantive_differences": ["Different operation"],
                            "algorithmic_significance": "Same complexity",
                        }
                    ]
                }
            }
            result = review_similarity(
                retrieval, self.stage("similarity_review", output)
            )
            self.assertEqual(result["manual"], relation == "essentially_equivalent")
            self.assertEqual(len(result["matches"]) + len(result["other"]), 1)

    def test_search_success_alone_does_not_establish_equivalence(self):
        retrieval = self.stage(
            "similarity_search", {"candidates": [{"ref": "candidate:1"}]}
        )
        result = review_similarity(
            retrieval, self.stage("similarity_review", {}, status="E")
        )
        self.assertTrue(result["incomplete"])

    def test_empty_retrieval_is_completed_but_never_guarantees_originality(self):
        result = review_similarity(
            self.stage("similarity_search", {"candidates": []}),
            self.stage("similarity_review", {"evidence": {"comparisons": []}}),
        )
        self.assertFalse(result["incomplete"])
        self.assertEqual(result["title"], "No candidate problems were returned.")


@override_settings(LANGUAGE_CODE="en")
class ReviewStageNotesTests(SimpleTestCase):
    def test_contract_and_independent_analysis_have_readable_results(self):
        contract = ReviewStage(
            stage_id="problem_contract",
            output_json={
                "task": "Compute a sum.",
                "input_structure": "An array.",
                "extractor": {"source": "private code"},
            },
        )
        notes = review_stage_notes(contract)
        self.assertEqual(
            [note["items"] for note in notes], [["Compute a sum."], ["An array."]]
        )
        analysis = ReviewStage(
            stage_id="independent_analysis",
            output_json={
                "F": {"complexity": ["O(n)"]},
                "G": {"coverage_blueprint": ["Maximum array length"]},
            },
        )
        self.assertEqual(
            [note["items"] for note in review_stage_notes(analysis)],
            [["O(n)"], ["Maximum array length"]],
        )

    def test_malformed_fields_do_not_leak_arbitrary_objects(self):
        for output in (
            None,
            [],
            {"errors": [{"secret": "hidden"}, None], "limitations": 42},
            {"F": [], "G": "invalid"},
        ):
            with self.subTest(output=output):
                self.assertEqual(
                    review_stage_notes(ReviewStage(stage_id="D", output_json=output)),
                    [],
                )


@override_settings(LANGUAGE_CODE="en")
class ReviewBadgeTests(SimpleTestCase):
    def test_blocked_runs_use_compact_recovery_label(self):
        for model in (ProblemReviewRun, ContestReviewRun):
            with self.subTest(model=model.__name__):
                self.assertEqual(
                    model(status="B").get_status_display(), "Needs recovery"
                )

    def test_section_colors_match_their_meaning(self):
        cases = [
            ("P", {}, "gray", "Pending"),
            ("R", {}, "blue", "Running"),
            ("E", {}, "red", "Error"),
            ("D", {}, "green", "Pass"),
            ("D", {"status": "no_issue"}, "green", "Pass"),
            ("D", {"complete": False}, "red", "Fail"),
            ("D", {"status": "uncertain"}, "orange", "Partial"),
            ("D", {"status": "finding"}, "orange", "Warning"),
            ("D", {"available": False}, "orange", "Partial"),
            (
                "D",
                {"limitations": ["Not all tests inspected"]},
                "orange",
                "Partial",
            ),
            (
                "D",
                {"status": "no_issue", "uncertainties": ["Unknown"]},
                "orange",
                "Partial",
            ),
            (
                "D",
                {"availability": "unavailable"},
                "orange",
                "Partial",
            ),
            (
                "D",
                {"status": "no_issue", "_missing_evidence": [{"stage_id": "FG"}]},
                "orange",
                "Partial",
            ),
            (
                "D",
                {"status": "no_issue", "findings": [{"severity": "blocking"}]},
                "red",
                "Fail",
            ),
            (
                "D",
                {"status": "no_issue", "findings": [{"severity": "warning"}]},
                "orange",
                "Warning",
            ),
        ]
        for status, output, tone, label in cases:
            with self.subTest(status=status, output=output):
                stage = ReviewStage(stage_id="H3", status=status, output_json=output)
                self.assertEqual(review_stage_badge(stage), (tone, label))

    def test_invalid_response_is_red_but_not_an_author_defect_label(self):
        stage = ReviewStage(status="E", error_category="invalid_response")
        self.assertEqual(review_stage_badge(stage), ("red", "Error"))

    def test_synthesis_badges_use_readiness_not_only_completion(self):
        for stage_id in ("final_assessment",):
            for readiness, tone, label in (
                ("READY", "green", "Pass"),
                ("READY_WITH_WARNINGS", "orange", "Warning"),
                ("NEEDS_AUTHOR_FIX", "red", "Fail"),
                ("MANUAL_REVIEW", "orange", "Partial"),
                ("INCOMPLETE", "orange", "Partial"),
            ):
                with self.subTest(stage_id=stage_id, readiness=readiness):
                    stage = ReviewStage(
                        stage_id=stage_id,
                        status="D",
                        output_json={"readiness": readiness},
                    )
                    self.assertEqual(review_stage_badge(stage), (tone, label))

    def test_unavailable_statistics_are_not_presented_as_pass(self):
        stage = ReviewStage(
            stage_id="extract_stats",
            status="D",
            output_json={"availability": "unavailable"},
        )
        self.assertEqual(review_stage_badge(stage), ("orange", "Partial"))

    def test_malformed_optional_data_does_not_break_badges(self):
        for output in (None, [], {"findings": [None, {"severity": []}]}):
            stage = ReviewStage(stage_id="H3", status="D", output_json=output)
            self.assertEqual(review_stage_badge(stage), ("green", "Pass"))


class ReviewProseTests(SimpleTestCase):
    def test_compact_child_lists_render_nested_for_both_list_types(self):
        for marker, spaces, root in (("-", 2, "ul"), ("1.", 3, "ol"), ("1.", 4, "ol")):
            with self.subTest(marker=marker, spaces=spaces):
                prose = (
                    marker
                    + " **Phạm vi:**\n"
                    + " " * spaces
                    + "- Biên nhỏ $N = 1$.\n"
                    + " " * spaces
                    + "- Biên lớn $N = 200\\,000$.\n\n"
                    + ("-" if root == "ul" else "2.")
                    + " **Phân loại:**\n"
                    + " " * spaces
                    + "- Kiểm tra `int`."
                )
                soup = BeautifulSoup(review_markdown(prose), "html.parser")
                top = soup.select_one(".md-typeset > " + root)
                self.assertEqual(len(top.select(":scope > li")), 2)
                first, second = top.select(":scope > li")
                self.assertEqual(len(first.select(":scope > ul > li")), 2)
                self.assertEqual(len(second.select(":scope > ul > li")), 1)
                self.assertEqual(len(soup.select(".arithmatex")), 2)
                self.assertEqual(soup.select_one("code").get_text(), "int")

    def test_compact_grandchildren_continuations_and_siblings(self):
        prose = (
            "- Parent\n  - Child\n    - Grandchild\n\n"
            "      Continuation.\n\n  - Second child\n- Sibling\n"
        )
        soup = BeautifulSoup(review_markdown(prose), "html.parser")
        root = soup.select_one(".md-typeset > ul")
        self.assertEqual(len(root.select(":scope > li")), 2)
        self.assertEqual(len(root.select(":scope > li > ul > li")), 2)
        grandchild = root.select_one(":scope > li > ul > li > ul > li")
        self.assertIn("Continuation.", grandchild.get_text())

    def test_standard_indentation_and_code_are_preserved(self):
        for prose in (
            "- Parent\n    - Child\n        - Grandchild\n    - Sibling\n- Next",
            "    - This is indented code\n        - Not a child list",
            "- Parent\n\n        - Indented code inside a list\n\n- Sibling",
            "```markdown\n- Parent\n  - Literal child\n```\n",
            "~~~text\n  - Literal list\n~~~~\n",
            "Use `- Parent  - Child` literally.",
            "- Flat\n- Still flat\n- No inferred hierarchy",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(review_list_indentation(prose), prose)
        self.assertEqual(review_list_indentation(None), "")

    def test_review_renderer_keeps_existing_sanitizer(self):
        html = review_markdown(
            "- Parent\n  - **Child** $x < y$ <script>bad()</script>\n"
            '  - [unsafe](javascript:bad) <img src="x" onerror="bad()">'
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("<strong>Child</strong>", html)
        self.assertIn('class="arithmatex"', html)

    def test_legacy_inline_list_gets_readable_markup(self):
        prose = (
            "Bộ test gồm: 1. Biên nhỏ, N = 1. 2. Tràn `int`, tổng 10^9. 3. Tải tối đa."
        )
        formatted = review_prose(prose)
        self.assertIn("\n\n1. Biên", formatted)
        html = str(markdown(formatted))
        self.assertIn("<ol>", html)
        self.assertEqual(html.count("<li>"), 3)
        self.assertIn("<code>int</code>", html)

    def test_existing_markdown_and_non_lists_are_unchanged(self):
        for prose in (
            "Summary\n\n- One\n- Two",
            "Time 0.047s; N = 1.",
            "Use `1. first 2. second` literally.",
            "See 2. then 4.",
        ):
            self.assertEqual(review_prose(prose), prose)
        self.assertEqual(review_prose(None), "")

    def test_model_markdown_is_sanitized_by_site_renderer(self):
        rendered = str(
            markdown(
                review_prose(
                    'Review <script>alert(1)</script> <img src=x onerror="alert(2)"> '
                    "[unsafe](javascript:alert(3))"
                )
            )
        )
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("onerror=", rendered)
        self.assertNotIn('href="javascript:', rendered)


class SemanticDatabaseRoutingTests(SimpleTestCase):
    @patch("judge.ml.semantic_search.connection")
    def test_default_query_still_uses_application_database(self, connection):
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (1, 0.5)
        ]
        with override_settings(SEMANTIC_SEARCH_DATABASE_ALIAS=None):
            self.assertEqual(
                semantic_search._semantic_vector_search("[1]", 5), [(1, 0.5)]
            )

    @patch("judge.ml.semantic_search.connection")
    @patch("judge.ml.semantic_search.connections")
    @patch("judge.ml.semantic_search.Problem.objects.filter")
    def test_explicit_vector_sidecar_keeps_privacy_filter_and_exclusion(
        self, problems, connections, connection
    ):
        cursor = (
            connections.__getitem__.return_value.cursor.return_value.__enter__.return_value
        )
        cursor.fetchall.return_value = [(7, 0.8), (8, 0.7)]
        problems.return_value.values_list.return_value = [7]
        with override_settings(SEMANTIC_SEARCH_DATABASE_ALIAS="semantic"):
            self.assertEqual(
                semantic_search._semantic_vector_search("[1]", 5, 10), [(7, 0.8)]
            )
        connections.__getitem__.assert_called_once_with("semantic")
        connection.cursor.assert_not_called()
        sql, params = cursor.execute.call_args.args
        self.assertNotIn("JOIN judge_problem", sql)
        self.assertIn("e.problem_id != %s", sql)
        self.assertIn(10, params)
        problems.assert_called_once_with(
            pk__in=[7, 8], is_public=True, is_organization_private=False
        )

    @patch("judge.ml.semantic_search.connection")
    @patch("judge.ml.semantic_search.connections")
    def test_sidecar_routes_index_reads_writes_errors_and_deletion(
        self, connections, connection
    ):
        cursor = (
            connections.__getitem__.return_value.cursor.return_value.__enter__.return_value
        )
        cursor.fetchone.return_value = ("stored",)
        with override_settings(SEMANTIC_SEARCH_DATABASE_ALIAS="semantic"):
            self.assertEqual(semantic_search._existing_content_hash(7), "stored")
            self.assertEqual(semantic_search._stored_problem_embedding(7), "stored")
            semantic_search._upsert_embedding(7, "hash", "[1]")
            semantic_search._record_index_error(7, "hash", "failed")
            semantic_search.prune_problem_embedding(7)
        connection.cursor.assert_not_called()
        self.assertEqual(cursor.execute.call_count, 8)
        for call in cursor.execute.call_args_list:
            self.assertNotIn("JOIN judge_problem", call.args[0])

    @patch("judge.ml.semantic_search.connection")
    @patch("judge.ml.semantic_search.connections")
    @patch("judge.ml.semantic_search.Problem.objects.filter")
    def test_sidecar_pruning_uses_authoritative_visibility_in_batches(
        self, problems, connections, connection
    ):
        cursor = (
            connections.__getitem__.return_value.cursor.return_value.__enter__.return_value
        )
        cursor.fetchall.side_effect = [[(7,), (8,)], [], [(9,)], []]
        cursor.rowcount = 1
        problems.return_value.values_list.side_effect = [[7], []]
        with override_settings(SEMANTIC_SEARCH_DATABASE_ALIAS="semantic"):
            result = semantic_search.prune_stale_embeddings()
        self.assertEqual(result, {"embeddings_deleted": 1, "errors_deleted": 1})
        self.assertEqual(problems.call_count, 2)
        connection.cursor.assert_not_called()
        deletes = [
            call
            for call in cursor.execute.call_args_list
            if "DELETE FROM" in call.args[0]
        ]
        self.assertEqual([call.args[1] for call in deletes], [[8], [9]])
