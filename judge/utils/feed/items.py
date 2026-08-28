class FeedItem:
    """A single item in the mixed feed."""

    POST = "post"
    COMMENT = "comment"
    PROBLEM = "problem"
    CONTEST = "contest"
    COURSES = "courses"
    QUIZZES = "quizzes"
    GROUPS = "groups"

    def __init__(self, item_type, data, time=None, content_key=None):
        self.item_type = item_type
        self.data = data
        self.time = time
        self.content_key = content_key
