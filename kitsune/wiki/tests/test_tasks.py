import re
from datetime import datetime
from unittest import mock

import bleach
import waffle
from django.conf import settings
from django.contrib.sites.models import Site
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.test.client import RequestFactory

from kitsune.kbadge.utils import get_or_create_badge
from kitsune.sumo.tests import TestCase
from kitsune.users.tests import UserFactory, add_permission
from kitsune.wiki.badges import WIKI_BADGES
from kitsune.wiki.config import TEMPLATE_TITLE_PREFIX, TEMPLATES_CATEGORY
from kitsune.wiki.models import Document, Revision
from kitsune.wiki.tasks import (
    _rebuild_kb_chunk,
    rebuild_kb,
    render_document_cascade,
    schedule_rebuild_kb,
    send_reviewed_notification,
)
from kitsune.wiki.tests import ApprovedRevisionFactory, RevisionFactory
from kitsune.wiki.tests.test_parser import doc_rev_parser

REVIEWED_EMAIL_CONTENT = """Your revision has been reviewed.

%s has approved your revision to the document %s.

Message from the reviewer:

%s

To view the history of this document, click the following link, or paste \
it into your browser's location bar:

https://testserver/en-US/kb/%s/history
"""


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class RebuildTestCase(TestCase):
    rf = RequestFactory()
    ALWAYS_EAGER = settings.CELERY_TASK_ALWAYS_EAGER

    def setUp(self):
        # create some random revisions.

        RevisionFactory()
        RevisionFactory(is_approved=True)
        RevisionFactory(is_approved=True)
        RevisionFactory(is_approved=True)
        RevisionFactory(is_approved=True)

    def tearDown(self):
        cache.delete(settings.WIKI_REBUILD_TOKEN)

    @mock.patch.object(rebuild_kb, "delay")
    @mock.patch.object(waffle, "switch_is_active")
    def test_eager_queue(self, switch_is_active, delay):
        switch_is_active.return_value = True
        schedule_rebuild_kb()
        assert not cache.get(settings.WIKI_REBUILD_TOKEN)
        assert not delay.called

    @mock.patch.object(rebuild_kb, "delay")
    @mock.patch.object(waffle, "switch_is_active")
    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_task_queue(self, switch_is_active, delay):
        switch_is_active.return_value = True
        schedule_rebuild_kb()
        assert cache.get(settings.WIKI_REBUILD_TOKEN)
        assert delay.called

    @mock.patch.object(rebuild_kb, "delay")
    @mock.patch.object(waffle, "switch_is_active")
    def test_already_queued(self, switch_is_active, delay):
        switch_is_active.return_value = True
        cache.set(settings.WIKI_REBUILD_TOKEN, True)
        schedule_rebuild_kb()
        assert cache.get(settings.WIKI_REBUILD_TOKEN)
        assert not delay.called

    @mock.patch.object(rebuild_kb, "delay")
    @mock.patch.object(cache, "get")
    @mock.patch.object(waffle, "switch_is_active")
    def test_dont_queue(self, switch_is_active, get, delay):
        switch_is_active.return_value = False
        schedule_rebuild_kb()
        assert not get.called
        assert not delay.called

    @mock.patch.object(_rebuild_kb_chunk, "apply_async")
    def test_rebuild_chunk(self, apply_async):
        cache.set(settings.WIKI_REBUILD_TOKEN, True)
        rebuild_kb()
        assert not cache.get(settings.WIKI_REBUILD_TOKEN)
        assert "args" in apply_async.call_args[1]
        # There should be 4 documents with an approved revision
        self.assertEqual(4, len(apply_async.call_args[1]["args"][0]))


class ReviewMailTestCase(TestCase):
    """Test that the review mail gets sent."""

    def setUp(self):
        self.user = UserFactory()
        add_permission(self.user, Revision, "review_revision")

    def _approve_and_send(self, revision, reviewer, message):
        revision.reviewer = reviewer
        revision.reviewed = datetime.now()
        revision.is_approved = True
        revision.save()
        send_reviewed_notification(revision.id, revision.document.id, message)

    @mock.patch.object(Site.objects, "get_current")
    def test_reviewed_notification(self, get_current):
        get_current.return_value.domain = "testserver"

        rev = RevisionFactory()
        doc = rev.document
        msg = "great work!"
        self._approve_and_send(rev, self.user, msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        self.assertEqual(2, len(mail.outbox))
        self.assertEqual(
            "Your revision has been approved: {}".format(doc.title), mail.outbox[0].subject
        )
        self.assertEqual([rev.creator.email], mail.outbox[0].to)
        self.assertEqual(
            REVIEWED_EMAIL_CONTENT % (self.user.profile.name, doc.title, msg, doc.slug),
            mail.outbox[0].body,
        )

    @mock.patch.object(Site.objects, "get_current")
    def test_reviewed_by_creator_no_notification(self, get_current):
        get_current.return_value.domain = "testserver"

        rev = RevisionFactory()
        msg = "great work!"
        self._approve_and_send(rev, rev.creator, msg)

        # Verify no email was sent
        self.assertEqual(0, len(mail.outbox))

    @mock.patch.object(Site.objects, "get_current")
    def test_unicode_notifications(self, get_current):
        get_current.return_value.domain = "testserver"

        rev = RevisionFactory()
        doc = rev.document
        doc.title = "Foo \xe8 incode"
        msg = "foo"
        self._approve_and_send(rev, self.user, msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        self.assertEqual(2, len(mail.outbox))
        self.assertEqual(
            "Your revision has been approved: {}".format(doc.title), mail.outbox[0].subject
        )

    @mock.patch.object(Site.objects, "get_current")
    def test_escaping(self, get_current):
        get_current.return_value.domain = "testserver"

        rev = RevisionFactory()
        doc = rev.document
        doc.title = '"All about quotes"'
        msg = 'foo & "bar"'
        self._approve_and_send(rev, self.user, msg)

        # Two emails will be sent, one each for the reviewer and the reviewed.
        self.assertEqual(2, len(mail.outbox))
        self.assertEqual(
            "Your revision has been approved: {}".format(doc.title), mail.outbox[0].subject
        )
        assert "&quot;" not in mail.outbox[0].body
        assert '"All about quotes"' in mail.outbox[0].body
        assert 'foo & "bar"' in mail.outbox[0].body


class TestDocumentRenderCascades(TestCase):
    def _clean(self, d):
        """Get a clean and normalized version of a documents html."""
        html = Document.objects.get(slug=d.slug).html
        return re.sub(r"\s+", " ", bleach.clean(html, strip=True)).strip()

    def test_cascade(self):
        d1, _, _ = doc_rev_parser(
            "one ", title=TEMPLATE_TITLE_PREFIX + "D1", category=TEMPLATES_CATEGORY
        )
        d2, _, _ = doc_rev_parser(
            "[[T:D1]] two", title=TEMPLATE_TITLE_PREFIX + "D2", category=TEMPLATES_CATEGORY
        )
        d3, _, _ = doc_rev_parser("[[T:D1]] [[T:D2]] three", title="D3")

        self.assertEqual(self._clean(d3), "one one two three")

        RevisionFactory(document=d1, content="ONE", is_approved=True)
        render_document_cascade(d1.id)

        self.assertEqual(self._clean(d1), "ONE")
        self.assertEqual(self._clean(d2), "ONE two")
        self.assertEqual(self._clean(d3), "ONE ONE two three")


class TestMaybeAwardBadge(TestCase):
    @override_settings(BADGE_LIMIT_L10N_KB=4)
    def test_maybe_award_badge_for_kb(self):
        badge = get_or_create_badge(
            WIKI_BADGES["kb-badge"],
            year=datetime.now().year,
        )

        rev1 = ApprovedRevisionFactory()
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev2 = ApprovedRevisionFactory(creator=rev1.creator)
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev3 = ApprovedRevisionFactory(creator=rev1.creator)
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        # These shouldn't count, since they're not approved.
        rev4 = RevisionFactory(creator=rev1.creator)
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev5 = RevisionFactory(creator=rev1.creator)
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev2.delete()
        rev3.delete()
        rev4.delete()
        rev5.delete()

        ApprovedRevisionFactory(creator=rev1.creator)
        self.assertTrue(badge.is_awarded_to(rev1.creator))

    @override_settings(BADGE_LIMIT_L10N_KB=4)
    def test_maybe_award_badge_for_l10n(self):
        badge = get_or_create_badge(
            WIKI_BADGES["l10n-badge"],
            year=datetime.now().year,
        )

        rev1 = ApprovedRevisionFactory(document__locale="de")
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev2 = ApprovedRevisionFactory(creator=rev1.creator, document__locale="es")
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev3 = ApprovedRevisionFactory(creator=rev1.creator, document__locale="it")
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        # These shouldn't count, since they're not approved.
        rev4 = RevisionFactory(creator=rev1.creator, document__locale="de")
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev5 = RevisionFactory(creator=rev1.creator, document__locale="es")
        self.assertFalse(badge.is_awarded_to(rev1.creator))

        rev2.delete()
        rev3.delete()
        rev4.delete()
        rev5.delete()

        ApprovedRevisionFactory(creator=rev1.creator, document__locale="fr")
        self.assertTrue(badge.is_awarded_to(rev1.creator))
