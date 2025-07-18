from django.contrib.sites.models import Site
from django.utils.translation import gettext_lazy as _lazy

from kitsune.forums.models import Forum, Thread
from kitsune.sumo.email_utils import emails_with_users_and_watches
from kitsune.sumo.templatetags.jinja_helpers import add_utm
from kitsune.tidings.events import EventUnion, InstanceEvent


class NewPostEvent(InstanceEvent):
    """An event which fires when a thread receives a reply

    Firing this also notifies watchers of the containing forum.

    """

    event_type = "thread reply"
    content_type = Thread

    def __init__(self, reply):
        super().__init__(reply.thread)
        # Need to store the reply for _mails
        self.reply = reply

    def send_emails(self, exclude=None):
        """Notify not only watchers of this thread but of the parent forum."""
        return EventUnion(self, NewThreadEvent(self.reply)).send_emails(exclude=exclude)

    def _mails(self, users_and_watches):
        post_url = add_utm(self.reply.get_absolute_url(), "forums-post")

        c = {
            "post": self.reply.content,
            "post_html": self.reply.content_parsed,
            "author": self.reply.author,
            "host": Site.objects.get_current().domain,
            "thread": self.reply.thread.title,
            "forum": self.reply.thread.forum.name,
            "post_url": post_url,
        }

        return emails_with_users_and_watches(
            subject=_lazy("Re: {forum} - {thread}"),
            text_template="forums/email/new_post.ltxt",
            html_template="forums/email/new_post.html",
            context_vars=c,
            users_and_watches=users_and_watches,
        )

    def serialize(self):
        """
        Serialize this event into a JSON-friendly dictionary.
        """
        return {
            "event": {"module": "kitsune.forums.events", "class": "NewPostEvent"},
            "instance": {"module": "kitsune.forums.models", "class": "Post", "id": self.reply.id},
        }


class NewThreadEvent(InstanceEvent):
    """An event which fires when a new thread is added to a forum"""

    event_type = "forum thread"
    content_type = Forum

    def __init__(self, post):
        super().__init__(post.thread.forum)
        # Need to store the post for _mails
        self.post = post

    def _mails(self, users_and_watches):
        post_url = add_utm(self.post.thread.get_absolute_url(), "forums-thread")

        c = {
            "post": self.post.content,
            "post_html": self.post.content_parsed,
            "author": self.post.author,
            "host": Site.objects.get_current().domain,
            "thread": self.post.thread.title,
            "forum": self.post.thread.forum.name,
            "post_url": post_url,
        }

        return emails_with_users_and_watches(
            subject=_lazy("{forum} - {thread}"),
            text_template="forums/email/new_thread.ltxt",
            html_template="forums/email/new_thread.html",
            context_vars=c,
            users_and_watches=users_and_watches,
        )

    def serialize(self):
        """
        Serialize this event into a JSON-friendly dictionary.
        """
        return {
            "event": {"module": "kitsune.forums.events", "class": "NewThreadEvent"},
            "instance": {"module": "kitsune.forums.models", "class": "Post", "id": self.post.id},
        }
