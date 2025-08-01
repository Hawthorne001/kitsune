import json
from functools import wraps
from textwrap import dedent
from unittest import mock

from django.contrib import messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test.client import RequestFactory
from django.test.utils import override_settings
from josepy import jwa, jwk, jws
from pyquery import PyQuery as pq

from kitsune.forums.models import Post, Thread
from kitsune.forums.tests import PostFactory, ThreadFactory
from kitsune.kbforums.models import Post as KBForumPost
from kitsune.kbforums.models import Thread as KBForumThread
from kitsune.kbforums.tests import (
    PostFactory as KBForumPostFactory,
)
from kitsune.kbforums.tests import (
    ThreadFactory as KBForumThreadFactory,
)
from kitsune.messages.models import InboxMessage, OutboxMessage
from kitsune.messages.utils import send_message
from kitsune.questions.models import Answer, Question
from kitsune.questions.tests import AnswerFactory, QuestionFactory
from kitsune.sumo.tests import TestCase
from kitsune.sumo.urlresolvers import reverse
from kitsune.users.models import AccountEvent, Deactivation, Profile, Setting, User
from kitsune.users.tests import GroupFactory, ProfileFactory, UserFactory, add_permission
from kitsune.users.views import edit_profile


class UserSettingsTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.profile = self.user.profile
        self.client.login(username=self.user.username, password="testpass")
        super().setUp()

    def test_create_setting(self):
        url = reverse("users.edit_settings", locale="en-US")
        self.assertEqual(Setting.objects.filter(user=self.user).count(), 0)  # No settings
        res = self.client.get(url, follow=True)
        self.assertEqual(200, res.status_code)
        res = self.client.post(url, {"forums_watch_new_thread": True}, follow=True)
        self.assertEqual(200, res.status_code)
        assert Setting.get_for_user(self.user, "forums_watch_new_thread")


class UserProfileTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.profile = self.user.profile
        self.userrl = reverse("users.profile", args=[self.user.username], locale="en-US")
        super().setUp()

    def test_ProfileFactory(self):
        res = self.client.get(self.userrl)
        self.assertContains(res, self.user.username)

    def test_profile_redirect(self):
        """Ensure that old profile URL's get redirected."""
        res = self.client.get(reverse("users.profile", args=[self.user.pk], locale="en-US"))
        self.assertEqual(302, res.status_code)

    def test_profile_inactive(self):
        """Inactive users don't have a public profile."""
        self.user.is_active = False
        self.user.save()
        res = self.client.get(self.userrl)
        self.assertEqual(404, res.status_code)

    def test_profile_post(self):
        res = self.client.post(self.userrl)
        self.assertEqual(405, res.status_code)

    def test_profile_deactivate(self):
        """Test user deactivation"""
        p = UserFactory().profile

        self.client.login(username=self.user.username, password="testpass")
        res = self.client.post(reverse("users.deactivate", locale="en-US"), {"user_id": p.user.id})

        self.assertEqual(403, res.status_code)

        add_permission(self.user, Profile, "deactivate_users")
        res = self.client.post(reverse("users.deactivate", locale="en-US"), {"user_id": p.user.id})

        self.assertEqual(302, res.status_code)

        log = Deactivation.objects.get(user_id=p.user_id)
        self.assertEqual(log.moderator_id, self.user.id)

        p = Profile.objects.get(user_id=p.user_id)
        assert not p.user.is_active

    def test_deactivate_and_flag_spam(self):
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        # Verify content is flagged as spam when requested.
        u = UserFactory()
        AnswerFactory(creator=u)
        QuestionFactory(creator=u)
        url = reverse("users.deactivate-spam", locale="en-US")
        res = self.client.post(url, {"user_id": u.id})

        self.assertEqual(302, res.status_code)
        self.assertEqual(1, Question.objects.filter(creator=u, is_spam=True).count())
        self.assertEqual(0, Question.objects.filter(creator=u, is_spam=False).count())
        self.assertEqual(1, Answer.objects.filter(creator=u, is_spam=True).count())
        self.assertEqual(0, Answer.objects.filter(creator=u, is_spam=False).count())

    def test_deactivate_spam_deletes_forum_posts_and_threads(self):
        """Test deactivating a user with mark_spam=True deletes forum content."""
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        spam_user = UserFactory()

        # Create forum threads and posts by the spam user
        ThreadFactory(creator=spam_user)
        PostFactory(author=spam_user)
        PostFactory(author=spam_user)

        # Create content by other users that should NOT be deleted
        other_user = UserFactory()
        ThreadFactory(creator=other_user)
        PostFactory(author=other_user)

        # Count actual content before deactivation
        spam_threads = Thread.objects.filter(creator=spam_user).count()
        spam_posts = Post.objects.filter(author=spam_user).count()
        other_threads = Thread.objects.filter(creator=other_user).count()
        other_posts = Post.objects.filter(author=other_user).count()

        # Verify content exists before deactivation
        self.assertGreater(spam_threads, 0)
        self.assertGreater(spam_posts, 0)
        self.assertGreater(other_threads, 0)
        self.assertGreater(other_posts, 0)

        # Deactivate user with spam flag
        url = reverse("users.deactivate-spam", locale="en-US")
        response = self.client.post(url, {"user_id": spam_user.id})

        # Verify successful deactivation
        self.assertEqual(302, response.status_code)
        spam_user.refresh_from_db()
        self.assertFalse(spam_user.is_active)

        # Verify all forum content by spam user is deleted
        self.assertEqual(0, Thread.objects.filter(creator=spam_user).count())
        self.assertEqual(0, Post.objects.filter(author=spam_user).count())

        # Verify content by other users remains intact
        self.assertEqual(other_threads, Thread.objects.filter(creator=other_user).count())
        self.assertEqual(other_posts, Post.objects.filter(author=other_user).count())

    def test_deactivate_spam_deletes_kbforum_posts_and_threads(self):
        """Test deactivating a user with mark_spam=True deletes kbforum content."""
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        spam_user = UserFactory()

        # Create kbforum threads and posts by the spam user
        KBForumThreadFactory(creator=spam_user)
        KBForumPostFactory(creator=spam_user)
        KBForumPostFactory(creator=spam_user)

        # Create content by other users that should NOT be deleted
        other_user = UserFactory()
        KBForumThreadFactory(creator=other_user)
        KBForumPostFactory(creator=other_user)

        # Verify content exists before deactivation
        self.assertEqual(1, KBForumThread.objects.filter(creator=spam_user).count())
        self.assertEqual(2, KBForumPost.objects.filter(creator=spam_user).count())
        self.assertEqual(1, KBForumThread.objects.filter(creator=other_user).count())
        self.assertEqual(1, KBForumPost.objects.filter(creator=other_user).count())

        # Deactivate user with spam flag
        url = reverse("users.deactivate-spam", locale="en-US")
        response = self.client.post(url, {"user_id": spam_user.id})

        # Verify successful deactivation
        self.assertEqual(302, response.status_code)
        spam_user.refresh_from_db()
        self.assertFalse(spam_user.is_active)

        # Verify all kbforum content by spam user is deleted
        self.assertEqual(0, KBForumThread.objects.filter(creator=spam_user).count())
        self.assertEqual(0, KBForumPost.objects.filter(creator=spam_user).count())

        # Verify content by other users remains intact
        self.assertEqual(1, KBForumThread.objects.filter(creator=other_user).count())
        self.assertEqual(1, KBForumPost.objects.filter(creator=other_user).count())

    def test_deactivate_without_spam_preserves_forum_content(self):
        """Test deactivating a user without mark_spam=True preserves forum content."""
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        user_to_deactivate = UserFactory()

        # Create forum threads and posts by the user
        ThreadFactory(creator=user_to_deactivate)
        PostFactory(author=user_to_deactivate)

        # Count content before deactivation
        threads_before = Thread.objects.filter(creator=user_to_deactivate).count()
        posts_before = Post.objects.filter(author=user_to_deactivate).count()

        # Verify content exists before deactivation
        self.assertGreater(threads_before, 0)
        self.assertGreater(posts_before, 0)

        # Deactivate user WITHOUT spam flag (regular deactivation)
        url = reverse("users.deactivate", locale="en-US")
        response = self.client.post(url, {"user_id": user_to_deactivate.id})

        # Verify successful deactivation
        self.assertEqual(302, response.status_code)
        user_to_deactivate.refresh_from_db()
        self.assertFalse(user_to_deactivate.is_active)

        # Verify forum content is preserved
        self.assertEqual(threads_before, Thread.objects.filter(creator=user_to_deactivate).count())
        self.assertEqual(posts_before, Post.objects.filter(author=user_to_deactivate).count())

    def test_deactivate_without_spam_preserves_kbforum_content(self):
        """Test deactivating user without mark_spam=True preserves kbforum content."""
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        user_to_deactivate = UserFactory()

        # Create kbforum threads and posts by the user
        KBForumThreadFactory(creator=user_to_deactivate)
        KBForumPostFactory(creator=user_to_deactivate)

        # Verify content exists before deactivation
        self.assertEqual(1, KBForumThread.objects.filter(creator=user_to_deactivate).count())
        self.assertEqual(1, KBForumPost.objects.filter(creator=user_to_deactivate).count())

        # Deactivate user WITHOUT spam flag (regular deactivation)
        url = reverse("users.deactivate", locale="en-US")
        response = self.client.post(url, {"user_id": user_to_deactivate.id})

        # Verify successful deactivation
        self.assertEqual(302, response.status_code)
        user_to_deactivate.refresh_from_db()
        self.assertFalse(user_to_deactivate.is_active)

        # Verify kbforum content is preserved
        self.assertEqual(1, KBForumThread.objects.filter(creator=user_to_deactivate).count())
        self.assertEqual(1, KBForumPost.objects.filter(creator=user_to_deactivate).count())

    def test_deactivate_spam_comprehensive_deletion(self):
        """Test comprehensive deletion of all content types when marking user as spam."""
        self.client.login(username=self.user.username, password="testpass")
        add_permission(self.user, Profile, "deactivate_users")

        spam_user = UserFactory()

        # Create forum content
        ThreadFactory(creator=spam_user)
        PostFactory(author=spam_user)

        # Create kbforum content
        KBForumThreadFactory(creator=spam_user)
        KBForumPostFactory(creator=spam_user)

        # Create questions/answers content
        QuestionFactory(creator=spam_user)
        AnswerFactory(creator=spam_user)

        # Count content before deactivation
        forum_threads = Thread.objects.filter(creator=spam_user).count()
        forum_posts = Post.objects.filter(author=spam_user).count()
        kb_threads = KBForumThread.objects.filter(creator=spam_user).count()
        kb_posts = KBForumPost.objects.filter(creator=spam_user).count()

        # Verify all content exists before deactivation
        self.assertGreater(forum_threads, 0)
        self.assertGreater(forum_posts, 0)
        self.assertGreater(kb_threads, 0)
        self.assertGreater(kb_posts, 0)
        self.assertEqual(1, Question.objects.filter(creator=spam_user).count())
        self.assertEqual(1, Answer.objects.filter(creator=spam_user).count())

        # Deactivate user with spam flag
        url = reverse("users.deactivate-spam", locale="en-US")
        response = self.client.post(url, {"user_id": spam_user.id})

        # Verify successful deactivation
        self.assertEqual(302, response.status_code)
        spam_user.refresh_from_db()
        self.assertFalse(spam_user.is_active)

        # Verify all forum/kbforum content is deleted
        self.assertEqual(0, Thread.objects.filter(creator=spam_user).count())
        self.assertEqual(0, Post.objects.filter(author=spam_user).count())
        self.assertEqual(0, KBForumThread.objects.filter(creator=spam_user).count())
        self.assertEqual(0, KBForumPost.objects.filter(creator=spam_user).count())

        # Verify questions/answers are marked as spam (not deleted)
        self.assertEqual(1, Question.objects.filter(creator=spam_user, is_spam=True).count())
        self.assertEqual(1, Answer.objects.filter(creator=spam_user, is_spam=True).count())


class ProfileNotificationTests(TestCase):
    """
    These tests confirm that FXA and non-FXA messages render properly.
    We use RequestFactory because the request object from self.client.request
    cannot be passed into messages.info()
    """

    def _get_request(self):
        user = UserFactory()
        request = RequestFactory().get(reverse("users.edit_profile", args=[user.username]))
        request.user = user
        request.LANGUAGE_CODE = "en"
        self.get_response = lambda *args, **kwargs: HttpResponse()

        middleware = SessionMiddleware(self.get_response)
        middleware.process_request(request)
        request.session.save()

        middleware = MessageMiddleware(self.get_response)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_fxa_notification_updated(self):
        request = self._get_request()
        messages.info(request, "fxa_notification_updated")
        response = edit_profile(request)
        doc = pq(response.content)
        self.assertEqual(1, len(doc("#fxa-notification-updated")))
        self.assertEqual(0, len(doc("#fxa-notification-created")))

    def test_non_fxa_notification_created(self):
        request = self._get_request()
        text = "This is a helpful piece of information"
        messages.info(request, text)
        response = edit_profile(request)
        doc = pq(response.content)
        self.assertEqual(0, len(doc("#fxa-notification-updated")))
        self.assertEqual(0, len(doc("#fxa-notification-created")))
        self.assertEqual(1, len(doc(".user-messages li")))
        self.assertEqual(doc(".user-messages li").text(), text)


class FXAAuthenticationTests(TestCase):
    def test_authenticate_does_not_update_session(self):
        self.client.get(reverse("users.fxa_authentication_init"))
        assert not self.client.session.get("is_contributor")

    def test_authenticate_does_update_session(self):
        url = reverse("users.fxa_authentication_init") + "?contributor=KB"
        self.client.get(url)
        assert self.client.session.get("contributor")


def setup_key(test):
    @wraps(test)
    def wrapper(self, *args, **kwargs):
        with mock.patch("kitsune.users.views.requests") as mock_requests:
            pem = dedent(
                """
            -----BEGIN RSA PRIVATE KEY-----
            MIIBOgIBAAJBAKx1c7RR7R/drnBSQ/zfx1vQLHUbFLh1AQQQ5R8DZUXd36efNK79
            vukFhN9HFoHZiUvOjm0c+pVE6K+EdE/twuUCAwEAAQJAMbrEnJCrQe8YqAbw1/Bn
            elAzIamndfE3U8bTavf9sgFpS4HL83rhd6PDbvx81ucaJAT/5x048fM/nFl4fzAc
            mQIhAOF/a9o3EIsDKEmUl+Z1OaOiUxDF3kqWSmALEsmvDhwXAiEAw8ljV5RO/rUp
            Zu2YMDFq3MKpyyMgBIJ8CxmGRc6gCmMCIGRQzkcmhfqBrhOFwkmozrqIBRIKJIjj
            8TRm2LXWZZ2DAiAqVO7PztdNpynugUy4jtbGKKjBrTSNBRGA7OHlUgm0dQIhALQq
            6oGU29Vxlvt3k0vmiRKU4AVfLyNXIGtcWcNG46h/
            -----END RSA PRIVATE KEY-----
            """
            )
            key = jwk.JWKRSA.load(pem.encode())
            pubkey = {"kty": "RSA", "alg": "RS256", "kid": "123"}
            pubkey.update(key.public_key().fields_to_partial_json())

            mock_json = mock.Mock()
            mock_json.json.return_value = {"keys": [pubkey]}
            mock_requests.get.return_value = mock_json
            test(self, key, *args, **kwargs)

    return wrapper


@override_settings(FXA_RP_CLIENT_ID="12345")
@override_settings(FXA_SET_ISSUER="http://example.com")
class WebhookViewTests(TestCase):
    def _call_webhook(self, events, key=None):
        payload = json.dumps(
            {
                "iss": "http://example.com",
                "sub": "54321",
                "aud": "12345",
                "iat": 1565720808,
                "jti": "e19ed6c5-4816-4171-aa43-56ffe80dbda1",
                "events": events,
            }
        )
        jwt = jws.JWS.sign(
            payload=payload.encode(),
            key=key,
            alg=jwa.RS256,
            kid="123",
            protect=frozenset(["alg", "kid"]),
        ).to_compact()
        return self.client.post(
            reverse("users.fxa_webhook"),
            content_type="",
            HTTP_AUTHORIZATION="Bearer " + jwt.decode(),
        )

    @mock.patch("kitsune.users.views.process_event_password_change")
    @setup_key
    def test_adds_event_to_db(self, key, process_mock):
        profile = ProfileFactory(fxa_uid="54321")
        events = {
            "https://schemas.accounts.firefox.com/event/password-change": {
                "changeTime": 1565721242227
            }
        }

        self.assertEqual(0, AccountEvent.objects.count())

        response = self._call_webhook(events, key)

        self.assertEqual(202, response.status_code)
        self.assertEqual(1, AccountEvent.objects.count())
        self.assertEqual(1, process_mock.delay.call_count)

        account_event = AccountEvent.objects.last()
        self.assertEqual(account_event.status, AccountEvent.UNPROCESSED)
        self.assertEqual(json.loads(account_event.body), list(events.values())[0])
        self.assertEqual(account_event.event_type, AccountEvent.PASSWORD_CHANGE)
        self.assertEqual(account_event.fxa_uid, "54321")
        self.assertEqual(account_event.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event.issued_at, "1565720808")
        self.assertEqual(account_event.profile, profile)

    @mock.patch("kitsune.users.views.process_event_profile_change")
    @mock.patch("kitsune.users.views.process_event_subscription_state_change")
    @setup_key
    def test_adds_multiple_events_to_db(
        self, key, process_subscription_mock, process_profile_mock
    ):
        profile = ProfileFactory(fxa_uid="54321")
        events = {
            "https://schemas.accounts.firefox.com/event/profile-change": {},
            "https://schemas.accounts.firefox.com/event/subscription-state-change": {
                "capabilities": ["capability_1", "capability_2"],
                "isActive": True,
                "changeTime": 1565721242227,
            },
        }

        self.assertEqual(0, AccountEvent.objects.count())

        response = self._call_webhook(events, key)

        self.assertEqual(202, response.status_code)
        self.assertEqual(2, AccountEvent.objects.count())
        self.assertEqual(1, process_subscription_mock.delay.call_count)
        self.assertEqual(1, process_profile_mock.delay.call_count)

        account_event_1 = AccountEvent.objects.get(event_type=AccountEvent.PROFILE_CHANGE)
        account_event_2 = AccountEvent.objects.get(
            event_type=AccountEvent.SUBSCRIPTION_STATE_CHANGE
        )

        self.assertEqual(json.loads(account_event_1.body), {})
        self.assertEqual(json.loads(account_event_2.body), list(events.values())[1])

        self.assertEqual(account_event_1.status, AccountEvent.UNPROCESSED)
        self.assertEqual(account_event_2.status, AccountEvent.UNPROCESSED)
        self.assertEqual(account_event_1.fxa_uid, "54321")
        self.assertEqual(account_event_2.fxa_uid, "54321")
        self.assertEqual(account_event_1.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event_2.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event_1.issued_at, "1565720808")
        self.assertEqual(account_event_2.issued_at, "1565720808")
        self.assertEqual(account_event_1.profile, profile)
        self.assertEqual(account_event_2.profile, profile)

    @mock.patch("kitsune.users.views.process_event_delete_user")
    @setup_key
    def test_handles_unknown_events(self, key, process_mock):
        profile = ProfileFactory(fxa_uid="54321")
        events = {
            "https://schemas.accounts.firefox.com/event/delete-user": {},
            "https://schemas.accounts.firefox.com/event/foobar": {},
            "barfoo": {},
        }

        self.assertEqual(0, AccountEvent.objects.count())

        response = self._call_webhook(events, key)

        self.assertEqual(202, response.status_code)
        self.assertEqual(3, AccountEvent.objects.count())
        self.assertEqual(1, process_mock.delay.call_count)

        account_event_1 = AccountEvent.objects.get(event_type=AccountEvent.DELETE_USER)
        account_event_2 = AccountEvent.objects.get(event_type="foobar")
        account_event_3 = AccountEvent.objects.get(event_type="barfoo")

        self.assertEqual(json.loads(account_event_1.body), {})
        self.assertEqual(json.loads(account_event_2.body), {})
        self.assertEqual(json.loads(account_event_3.body), {})
        self.assertEqual(account_event_1.status, AccountEvent.UNPROCESSED)
        self.assertEqual(account_event_2.status, AccountEvent.NOT_IMPLEMENTED)
        self.assertEqual(account_event_3.status, AccountEvent.NOT_IMPLEMENTED)
        self.assertEqual(account_event_1.fxa_uid, "54321")
        self.assertEqual(account_event_2.fxa_uid, "54321")
        self.assertEqual(account_event_3.fxa_uid, "54321")
        self.assertEqual(account_event_1.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event_2.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event_3.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event_1.issued_at, "1565720808")
        self.assertEqual(account_event_2.issued_at, "1565720808")
        self.assertEqual(account_event_3.issued_at, "1565720808")
        self.assertEqual(account_event_1.profile, profile)
        self.assertEqual(account_event_2.profile, profile)
        self.assertEqual(account_event_3.profile, profile)

    @mock.patch("kitsune.users.views.process_event_delete_user")
    @setup_key
    def test_handles_no_user(self, key, process_mock):
        events = {"https://schemas.accounts.firefox.com/event/delete-user": {}}

        self.assertEqual(0, AccountEvent.objects.count())

        response = self._call_webhook(events, key)

        self.assertEqual(202, response.status_code)
        self.assertEqual(1, AccountEvent.objects.count())
        self.assertEqual(0, process_mock.delay.call_count)

        account_event = AccountEvent.objects.last()
        self.assertEqual(account_event.status, AccountEvent.IGNORED)
        self.assertEqual(json.loads(account_event.body), {})
        self.assertEqual(account_event.event_type, AccountEvent.DELETE_USER)
        self.assertEqual(account_event.fxa_uid, "54321")
        self.assertEqual(account_event.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        self.assertEqual(account_event.issued_at, "1565720808")
        self.assertEqual(account_event.profile, None)

    @setup_key
    def test_invalid_private_key(self, key):
        payload = json.dumps(
            {
                "iss": "http://example.com",
                "sub": "54321",
                "aud": "12345",
                "iat": 1565720808,
                "jti": "e19ed6c5-4816-4171-aa43-56ffe80dbda1",
                "events": {
                    "https://schemas.accounts.firefox.com/event/password-change": {
                        "changeTime": 1565721242227
                    }
                },
            }
        )

        pem = dedent(
            """
        -----BEGIN RSA PRIVATE KEY-----
        MIIBOwIBAAJBAK+qEuIq8XPibyHVbTqk/mPinYKq1CwkPGIs+KjYSUCJdgBIATna
        uETAv/tmlSuyWi+S2RZGqHfrPSnclZ/zFlECAwEAAQJAKmlmm8KAf1kpOcL8107k
        uJsLKnQyO+IXziBLfQCTVwgyoggtyhFgrm+r81/j8bHYAGuPLkOxSoTLgw36ziZH
        wQIhAOHRxpQmT/CQPKt7kvoFa9IOo+mu0CmRsfpQThz3kq2pAiEAxyRIk3cOapT6
        NnVQFPvj2EqNgwYl+uhj6I6wPTbMfGkCIQCrQdJd7KhXgqvgSTlwD8hzZ9L7mC4a
        OHpHobt70G4W8QIhAI1/BGpzP7UPYbHsLRib2crHPkGIzte247ZMHIGCPE1xAiBc
        dDwTPfi5ZdAouUH+T4RCqgS5lrNSB4yah8LxFwFpVg==
        -----END RSA PRIVATE KEY-----
        """
        )
        key = jwk.JWKRSA.load(pem.encode())

        jwt = jws.JWS.sign(
            payload=payload.encode(),
            key=key,
            alg=jwa.RS256,
            kid="123",
            protect=frozenset(["alg", "kid"]),
        ).to_compact()

        self.assertEqual(0, AccountEvent.objects.count())

        response = self.client.post(
            reverse("users.fxa_webhook"),
            content_type="",
            HTTP_AUTHORIZATION="Bearer " + jwt.decode(),
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, AccountEvent.objects.count())

    @setup_key
    def test_id_token(self, key):
        payload = json.dumps(
            {
                "iss": "http://example.com",
                "sub": "54321",
                "aud": "12345",
                "iat": 1565720808,
            }
        )

        jwt = jws.JWS.sign(
            payload=payload.encode(),
            key=key,
            alg=jwa.RS256,
            kid="123",
            protect=frozenset(["alg", "kid"]),
        ).to_compact()

        self.assertEqual(0, AccountEvent.objects.count())

        response = self.client.post(
            reverse("users.fxa_webhook"),
            content_type="",
            HTTP_AUTHORIZATION="Bearer " + jwt.decode(),
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, AccountEvent.objects.count())


class UserCloseAccountTests(TestCase):
    def setUp(self):
        self.user = UserFactory(
            username="ringo", email="ringo@beatles.com", groups=[GroupFactory()]
        )
        self.client.login(username=self.user.username, password="testpass")
        # Populate inboxes and outboxes with messages between the user and other users.
        self.other_users = UserFactory.create_batch(2)
        for sender in self.other_users:
            send_message({"users": [self.user]}, text="foo", sender=sender)
        send_message({"users": self.other_users}, text="bar", sender=self.user)
        super().setUp()

    def tearDown(self):
        User.objects.all().delete()
        InboxMessage.objects.all().delete()
        OutboxMessage.objects.all().delete()

    def test_close_account(self):
        """Test the closing of a user's account."""
        # Confirm the expected initial state.
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.profile.name)

        res = self.client.post(reverse("users.close_account", locale="en-US"))
        self.assertEqual(200, res.status_code)

        # The user should be completely deleted
        with self.assertRaises(User.DoesNotExist):
            self.user.refresh_from_db()
