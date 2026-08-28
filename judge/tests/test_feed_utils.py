from types import SimpleNamespace

from django.test import SimpleTestCase

from judge.utils.feed.cursor import FeedCursor
from judge.utils.feed.generator import FeedGenerator
from judge.utils.feed.items import FeedItem
from judge.utils.feed.pools import _feed_cache_key


class FakePool:
    def __init__(self, items):
        self.items = items

    def get(self, offset, count):
        return self.items[offset : offset + count]

    def has_more(self, offset):
        return offset < len(self.items)


class FeedGeneratorDedupTests(SimpleTestCase):
    def _item(self, item_type, key):
        return FeedItem(item_type, SimpleNamespace(id=key[1]), content_key=key)

    def _generator(self, *, posts=(), comments=(), problems=(), contests=(), groups=()):
        generator = FeedGenerator.__new__(FeedGenerator)
        generator.post_pool = FakePool(list(posts))
        generator.comment_pool = FakePool(list(comments))
        generator.problem_pool = FakePool(list(problems))
        generator.contest_pool = FakePool(list(contests))
        generator.group_pool = FakePool(list(groups))
        return generator

    def test_comment_activity_duplicate_of_post_is_skipped(self):
        post_key = (1, 10)
        unique_comment_key = (1, 11)
        generator = self._generator(
            posts=[
                self._item(FeedItem.POST, post_key),
                self._item(FeedItem.POST, (1, 12)),
                self._item(FeedItem.POST, (1, 13)),
                self._item(FeedItem.POST, (1, 14)),
            ],
            comments=[
                self._item(FeedItem.COMMENT, post_key),
                self._item(FeedItem.COMMENT, unique_comment_key),
            ],
        )

        items, cursor = generator.generate_page(FeedCursor(), 4)

        self.assertEqual(
            [item.content_key for item in items],
            [post_key, (1, 12), (1, 13), unique_comment_key],
        )
        self.assertEqual(cursor.post_offset, 3)
        self.assertEqual(cursor.comment_offset, 2)

    def test_cursor_prevents_duplicates_on_later_pages(self):
        duplicate_key = (2, 20)
        unique_key = (2, 21)
        generator = self._generator(
            comments=[
                self._item(FeedItem.COMMENT, duplicate_key),
                self._item(FeedItem.COMMENT, unique_key),
                self._item(FeedItem.COMMENT, (2, 22)),
            ]
        )

        items, cursor = generator.generate_page(
            FeedCursor(slot_offset=3, seen_content_keys=[duplicate_key]), 1
        )

        self.assertEqual([item.content_key for item in items], [unique_key])
        self.assertEqual(cursor.comment_offset, 2)

    def test_problem_and_contest_cards_dedupe_against_comment_activity(self):
        problem_key = (3, 30)
        contest_key = (4, 40)
        generator = self._generator(
            comments=[
                self._item(FeedItem.COMMENT, problem_key),
                self._item(FeedItem.COMMENT, contest_key),
            ],
            problems=[self._item(FeedItem.PROBLEM, problem_key)],
            contests=[
                self._item(FeedItem.CONTEST, contest_key),
                self._item(FeedItem.CONTEST, (4, 41)),
                self._item(FeedItem.CONTEST, (4, 42)),
            ],
        )

        items, cursor = generator.generate_page(FeedCursor(slot_offset=4), 3)

        self.assertEqual(
            [item.content_key for item in items],
            [problem_key, contest_key, (4, 41)],
        )
        self.assertEqual(cursor.problem_offset, 1)
        self.assertEqual(cursor.comment_offset, 2)
        self.assertEqual(cursor.contest_offset, 2)


class FeedCacheKeyTests(SimpleTestCase):
    def test_feed_cache_key_is_scoped_by_organization(self):
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True),
            profile=SimpleNamespace(id=7),
            GET={"ft": "token"},
        )
        first_org = SimpleNamespace(id=3152)
        second_org = SimpleNamespace(id=4000)

        self.assertNotEqual(
            _feed_cache_key(request, "post", first_org),
            _feed_cache_key(request, "post", second_org),
        )
        self.assertNotEqual(
            _feed_cache_key(request, "post", first_org),
            _feed_cache_key(request, "post", None),
        )

    def test_feed_cursor_round_trips_seen_content_keys(self):
        cursor = FeedCursor(seen_content_keys=[(1, 10), (2, 20)])

        decoded = FeedCursor.decode(cursor.encode())

        self.assertEqual(decoded.seen_content_keys, [(1, 10), (2, 20)])
