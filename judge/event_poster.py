from django.conf import settings

__all__ = ["last", "post", "post_many"]

if not settings.EVENT_DAEMON_USE:
    real = False

    def post(channel, message):
        return 0

    def post_many(events):
        return 0

    def last():
        return 0

elif hasattr(settings, "EVENT_DAEMON_AMQP"):
    from .event_poster_amqp import last, post, post_many

    real = True
else:
    from .event_poster_ws import last, post, post_many

    real = True
