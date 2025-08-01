import json
from datetime import datetime, timedelta
from unittest import mock

import waffle
from actstream.models import Action, Follow
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db.models import Q

import kitsune.sumo.models
from kitsune.flagit.models import FlaggedObject
from kitsune.products.tests import ProductFactory, TopicFactory
from kitsune.questions.models import (
    AlreadyTakenException,
    Answer,
    InvalidUserException,
    Question,
    QuestionMetaData,
    QuestionVisits,
    VoteMetadata,
    _has_beta,
    _tenths_version,
)
from kitsune.questions.tasks import update_answer_pages
from kitsune.questions.tests import (
    AnswerFactory,
    AnswerVoteFactory,
    QuestionFactory,
    QuestionVoteFactory,
    tags_eq,
)
from kitsune.search.tests import ElasticTestCase
from kitsune.sumo import googleanalytics
from kitsune.sumo.tests import TestCase
from kitsune.tags.models import SumoTag
from kitsune.tags.tests import TagFactory
from kitsune.tags.utils import add_existing_tag
from kitsune.users.tests import UserFactory
from kitsune.wiki.tests import TranslatedRevisionFactory


class TestAnswer(TestCase):
    """Test the Answer model"""

    def test_new_answer_updates_question(self):
        """Test saving a new answer updates the corresponding question.
        Specifically, last_post and num_replies should update."""
        q = QuestionFactory(title="Test Question", content="Lorem Ipsum Dolor")
        updated = q.updated

        self.assertEqual(0, q.num_answers)
        self.assertEqual(None, q.last_answer)

        a = AnswerFactory(question=q, content="Test Answer")
        a.save()

        q = Question.objects.get(pk=q.id)
        self.assertEqual(1, q.num_answers)
        self.assertEqual(a, q.last_answer)
        self.assertNotEqual(updated, q.updated)

    def test_delete_question_removes_flag(self):
        """Deleting a question also removes the flags on that question."""
        q = QuestionFactory(title="Test Question", content="Lorem Ipsum Dolor")

        u = UserFactory()
        FlaggedObject.objects.create(
            status=0, content_object=q, reason="language", creator_id=u.id
        )
        self.assertEqual(1, FlaggedObject.objects.filter(reason="language").count())

        q.delete()
        self.assertEqual(0, FlaggedObject.objects.filter(reason="language").count())

    def test_delete_answer_removes_flag(self):
        """Deleting an answer also removes the flags on that answer."""
        q = QuestionFactory(title="Test Question", content="Lorem Ipsum Dolor")

        a = AnswerFactory(question=q, content="Test Answer")

        u = UserFactory()
        FlaggedObject.objects.create(
            status=0, content_object=a, reason="language", creator_id=u.id
        )
        content_type = ContentType.objects.get_for_model(Answer)
        self.assertEqual(1, FlaggedObject.objects.filter(content_type=content_type).count())

        a.delete()
        self.assertEqual(0, FlaggedObject.objects.filter(content_type=content_type).count())

    def test_delete_last_answer_of_question(self):
        """Deleting the last_answer of a Question should update the question."""
        yesterday = datetime.now() - timedelta(days=1)
        q = AnswerFactory(created=yesterday).question
        last_answer = q.last_answer

        # add a new answer and verify last_answer updated
        a = AnswerFactory(question=q, content="Test Answer")
        q = Question.objects.get(pk=q.id)

        self.assertEqual(q.last_answer.id, a.id)

        # delete the answer and last_answer should go back to previous value
        a.delete()
        q = Question.objects.get(pk=q.id)
        self.assertEqual(q.last_answer.id, last_answer.id)
        self.assertEqual(Answer.objects.filter(pk=a.id).count(), 0)

    def test_delete_solution_of_question(self):
        """Deleting the solution of a Question should update the question."""
        # set a solution to the question
        q = AnswerFactory().question
        solution = q.last_answer
        q.solution = solution
        q.save()

        # delete the solution and question.solution should go back to None
        solution.delete()
        q = Question.objects.get(pk=q.id)
        self.assertEqual(q.solution, None)

    def test_update_page_task(self):
        a = AnswerFactory()
        a.page = 4
        a.save()
        a = Answer.objects.get(pk=a.id)
        assert a.page == 4
        update_answer_pages(a.question.id)
        a = Answer.objects.get(pk=a.id)
        assert a.page == 1

    def test_delete_updates_pages(self):
        a1 = AnswerFactory()
        a2 = AnswerFactory(question=a1.question)
        AnswerFactory(question=a1.question)
        a1.page = 7
        a1.save()
        a2.delete()
        a3 = Answer.objects.filter(question=a1.question)[0]
        assert a3.page == 1, "Page was {}".format(a3.page)

    def test_creator_num_answers(self):
        a = AnswerFactory()

        self.assertEqual(a.creator_num_answers, 1)

        AnswerFactory(creator=a.creator)
        self.assertEqual(a.creator_num_answers, 2)

    def test_creator_num_solutions(self):
        a = AnswerFactory()
        q = a.question

        q.solution = a
        q.save()

        self.assertEqual(a.creator_num_solutions, 1)

    def test_content_parsed_with_locale(self):
        """Make sure links to localized articles work."""
        rev = TranslatedRevisionFactory(
            is_approved=True, document__title="Un mejor títuolo", document__locale="es"
        )

        a = AnswerFactory(question__locale="es", content="[[{}]]".format(rev.document.title))

        assert "es/kb/{}".format(rev.document.slug) in a.content_parsed

    def test_creator_follows(self):
        a = AnswerFactory()
        follows = Follow.objects.filter(user=a.creator)

        # It's a pain to filter this from the DB, since follow_object is a
        # ContentType field, so instead, do it in Python.
        self.assertEqual(len(follows), 2)
        answer_follow = [f for f in follows if f.follow_object == a][0]
        question_follow = [f for f in follows if f.follow_object == a.question][0]

        self.assertEqual(question_follow.actor_only, False)
        self.assertEqual(answer_follow.actor_only, False)


class TestQuestionMetadata(TestCase):
    """Tests handling question metadata"""

    def setUp(self):
        super().setUp()

        # add a new Question to test with
        self.question = QuestionFactory(title="Test Question", content="Lorem Ipsum Dolor")

    def test_add_metadata(self):
        """Test the saving of metadata."""
        metadata = {"version": "3.6.3", "os": "Windows 7"}
        self.question.add_metadata(**metadata)
        saved = QuestionMetaData.objects.filter(question=self.question)
        self.assertEqual({x.name: x.value for x in saved}, metadata)

    def test_metadata_property(self):
        """Test the metadata property on Question model."""
        self.question.add_metadata(crash_id="1234567890")
        self.assertEqual("1234567890", self.question.metadata["crash_id"])

    def test_clear_mutable_metadata(self):
        """Make sure it works and clears the internal cache.

        crash_id should get cleared, while product, category, and useragent
        should remain.

        """
        q = self.question
        q.add_metadata(
            product="desktop",
            category="fix-problems",
            useragent="Fyerfocks",
            crash_id="7",
            kb_visits_prior='["/en-US/kb/stuff", "/en-US/kb/nonsense"]',
        )

        q.metadata
        q.clear_mutable_metadata()
        md = q.metadata
        assert "crash_id" not in md, "clear_mutable_metadata() didn't clear the cached metadata."
        self.assertEqual(
            {
                "product": "desktop",
                "category": "fix-problems",
                "useragent": "Fyerfocks",
                "kb_visits_prior": '["/en-US/kb/stuff", "/en-US/kb/nonsense"]',
            },
            md,
        )

    def test_auto_tagging(self):
        """Make sure tags get applied based on metadata on first save."""
        SumoTag.objects.get_or_create(name="green", defaults={"slug": "green"})
        SumoTag.objects.get_or_create(name="Troubleshooting", defaults={"slug": "troubleshooting"})
        SumoTag.objects.get_or_create(name="Firefox", defaults={"slug": "firefox"})
        q = self.question
        q.product = ProductFactory(slug="firefox")
        q.topic = TopicFactory(slug="troubleshooting")
        q.add_metadata(ff_version="3.6.8", os="GREen")
        q.save()
        q.auto_tag()
        tags_eq(q, ["firefox", "troubleshooting", "Firefox 3.6.8", "Firefox 3.6", "green"])

    def test_auto_tagging_aurora(self):
        """Make sure versions with prerelease suffix are tagged properly."""
        q = self.question
        q.add_metadata(ff_version="18.0a2")
        q.save()
        q.auto_tag()
        tags_eq(q, ["Firefox 18.0"])

    def test_auto_tagging_restraint(self):
        """Auto-tagging shouldn't tag unknown Firefox versions or OSes."""
        q = self.question
        q.add_metadata(ff_version="allyourbase", os="toaster 1.0")
        q.save()
        q.auto_tag()
        tags_eq(q, [])

    def test_tenths_version(self):
        """Test the filter that turns 1.2.3 into 1.2."""
        self.assertEqual(_tenths_version("1.2.3beta3"), "1.2")
        self.assertEqual(_tenths_version("1.2rc"), "1.2")
        self.assertEqual(_tenths_version("1.w"), "")

    def test_has_beta(self):
        """Test the _has_beta helper."""
        assert _has_beta("5.0", {"5.0b3": "2011-06-01"})
        assert not _has_beta("6.0", {"5.0b3": "2011-06-01"})
        assert not _has_beta("5.5", {"5.0b3": "2011-06-01"})
        assert _has_beta("5.7", {"5.7b1": "2011-06-01"})
        assert _has_beta("11.0", {"11.0b7": "2011-06-01"})
        assert not _has_beta("10.0", {"11.0b7": "2011-06-01"})

    def test_kb_visits_prior(self):
        visits = ["/en-US/kb/stuff", "/en-US/kb/nonsense"]
        self.question.add_metadata(kb_visits_prior=json.dumps(visits))
        self.assertTrue(self.question.created_after_failed_kb_deflection)
        self.assertEqual(set(self.question.kb_visits_prior_to_creation), set(visits))

    def test_no_kb_visits_prior(self):
        self.assertFalse(self.question.created_after_failed_kb_deflection)
        self.assertEqual(self.question.kb_visits_prior_to_creation, [])


class QuestionTests(TestCase):
    """Tests for Question model"""

    def test_save_updated(self):
        """Saving with the `update` option should update `updated`."""
        q = QuestionFactory()
        updated = q.updated
        q.save(update=True)
        self.assertNotEqual(updated, q.updated)

    def test_save_no_update(self):
        """Saving without the `update` option shouldn't update `updated`."""
        q = QuestionFactory()
        updated = q.updated
        q.save()
        self.assertEqual(updated, q.updated)

    def test_default_manager(self):
        """Assert Question's default manager is SUMO's ManagerBase.

        This is easy to get wrong when mixing in taggability.

        """
        self.assertEqual(
            Question._default_manager.__class__,
            kitsune.questions.managers.QuestionManager,
        )

    def test_is_solved_property(self):
        a = AnswerFactory()
        q = a.question
        assert not q.is_solved
        q.solution = a
        q.save()
        assert q.is_solved

    def test_recent_counts(self):
        """Verify recent_asked_count and recent unanswered count."""
        # create a question for each of past 4 days
        now = datetime.now()
        QuestionFactory(created=now)
        QuestionFactory(created=now - timedelta(hours=12), is_locked=True)
        q = QuestionFactory(created=now - timedelta(hours=23))
        AnswerFactory(question=q)
        # 25 hours instead of 24 to avoid random test fails.
        QuestionFactory(created=now - timedelta(hours=25))

        # Only 3 are recent from last 72 hours, 1 has an answer.
        self.assertEqual(3, Question.recent_asked_count())
        self.assertEqual(1, Question.recent_unanswered_count())

    def test_recent_counts_with_filter(self):
        """Verify that recent_asked_count and recent_unanswered_count
        respect filters passed."""

        now = datetime.now()
        QuestionFactory(created=now, locale="en-US")
        q = QuestionFactory(created=now, locale="en-US")
        AnswerFactory(question=q)

        QuestionFactory(created=now, locale="pt-BR")
        QuestionFactory(created=now, locale="pt-BR")
        q = QuestionFactory(created=now, locale="pt-BR")
        AnswerFactory(question=q)

        # 5 asked recently, 3 are unanswered
        self.assertEqual(5, Question.recent_asked_count())
        self.assertEqual(3, Question.recent_unanswered_count())

        # check english (2 asked, 1 unanswered)
        locale_filter = Q(locale="en-US")
        self.assertEqual(2, Question.recent_asked_count(locale_filter))
        self.assertEqual(1, Question.recent_unanswered_count(locale_filter))

        # check pt-BR (3 asked, 2 unanswered)
        locale_filter = Q(locale="pt-BR")
        self.assertEqual(3, Question.recent_asked_count(locale_filter))
        self.assertEqual(2, Question.recent_unanswered_count(locale_filter))

    def test_from_url(self):
        """Verify question returned from valid URL."""
        q = QuestionFactory()

        self.assertEqual(q, Question.from_url("/en-US/questions/{}".format(q.id)))
        self.assertEqual(q, Question.from_url("/es/questions/{}".format(q.id)))

    def test_from_url_id_only(self):
        """Verify question returned from URL."""
        # When requesting the id, the existence of the question isn't checked.
        self.assertEqual(123, Question.from_url("/en-US/questions/123", id_only=True))
        self.assertEqual(234, Question.from_url("/es/questions/234", id_only=True))
        self.assertEqual(None, Question.from_url("/questions/345", id_only=True))

    def test_from_invalid_url(self):
        """Verify question returned from valid URL."""
        q = QuestionFactory()

        self.assertEqual(None, Question.from_url("/questions/{}".format(q.id)))
        self.assertEqual(None, Question.from_url("/en-US/questions/{}/edit".format(q.id)))
        self.assertEqual(None, Question.from_url("/en-US/kb/{}".format(q.id)))
        self.assertEqual(None, Question.from_url("/random/url"))
        self.assertEqual(None, Question.from_url("/en-US/questions/dashboard/metrics"))

    def test_editable(self):
        q = QuestionFactory()
        assert q.editable  # unlocked/unarchived
        q.is_archived = True
        assert not q.editable  # unlocked/archived
        q.is_locked = True
        assert not q.editable  # locked/archived
        q.is_archived = False
        assert not q.editable  # locked/unarchived
        q.is_locked = False
        assert q.editable  # unlocked/unarchived

    def test_age(self):
        now = datetime.now()
        ten_days_ago = now - timedelta(days=10)
        thirty_seconds_ago = now - timedelta(seconds=30)

        q1 = QuestionFactory(created=ten_days_ago)
        q2 = QuestionFactory(created=thirty_seconds_ago)

        # This test relies on datetime.now() being called in the age
        # property, so this delta check makes it less likely to fail
        # randomly.
        assert abs(q1.age - 10 * 24 * 60 * 60) < 2, "q1.age ({}) != 10 days".format(q1.age)
        assert abs(q2.age - 30) < 2, "q2.age ({}) != 30 seconds".format(q2.age)

    def test_is_taken(self):
        q = QuestionFactory()
        u = UserFactory()
        self.assertEqual(q.is_taken, False)

        q.taken_by = u
        q.taken_until = datetime.now() + timedelta(seconds=600)
        q.save()
        self.assertEqual(q.is_taken, True)

        q.taken_by = None
        q.taken_until = None
        q.save()
        self.assertEqual(q.is_taken, False)

    def test_take(self):
        u = UserFactory()
        q = QuestionFactory()
        q.take(u)
        self.assertEqual(q.taken_by, u)
        assert q.taken_until is not None

    def test_take_creator(self):
        q = QuestionFactory()
        with self.assertRaises(InvalidUserException):
            q.take(q.creator)

    def test_take_twice_fails(self):
        u1 = UserFactory()
        u2 = UserFactory()
        q = QuestionFactory()
        q.take(u1)
        with self.assertRaises(AlreadyTakenException):
            q.take(u2)

    def test_take_twice_same_user_refreshes_time(self):
        u = UserFactory()
        first_taken_until = datetime.now() - timedelta(minutes=5)
        q = QuestionFactory(taken_by=u, taken_until=first_taken_until)
        q.take(u)
        assert q.taken_until > first_taken_until

    def test_take_twice_forced(self):
        u1 = UserFactory()
        u2 = UserFactory()
        q = QuestionFactory()
        q.take(u1)
        q.take(u2, force=True)
        self.assertEqual(q.taken_by, u2)

    def test_taken_until_is_set(self):
        u = UserFactory()
        q = QuestionFactory()
        q.take(u)
        assert q.taken_until > datetime.now()

    def test_is_taken_clears(self):
        u = UserFactory()
        taken_until = datetime.now() - timedelta(seconds=30)
        q = QuestionFactory(taken_by=u, taken_until=taken_until)
        # Testin q.is_taken should clear out ``taken_by`` and ``taken_until``,
        # since taken_until is in the past.
        self.assertEqual(q.is_taken, False)
        self.assertEqual(q.taken_by, None)
        self.assertEqual(q.taken_until, None)

    def test_creator_follows(self):
        q = QuestionFactory()
        f = Follow.objects.get(user=q.creator)
        self.assertEqual(f.follow_object, q)
        self.assertEqual(f.actor_only, False)

    def test_helpful_replies(self):
        """Verify the "helpful_replies" property."""
        answer1 = AnswerFactory()
        question = answer1.question
        AnswerVoteFactory(answer=answer1, helpful=False)
        answer2 = AnswerFactory(question=question)
        AnswerVoteFactory(answer=answer2, helpful=True)
        AnswerVoteFactory(answer=answer2, helpful=False)
        with self.subTest("ignore answers with no helpful votes"):
            self.assertEqual(list(question.helpful_replies), [answer2])
        answer3 = AnswerFactory(question=question)
        AnswerVoteFactory(answer=answer3, helpful=True)
        AnswerVoteFactory(answer=answer3, helpful=False)
        AnswerVoteFactory(answer=answer3, helpful=True)
        answer4 = AnswerFactory(question=question)
        AnswerVoteFactory(answer=answer4, helpful=True)
        AnswerVoteFactory(answer=answer4, helpful=False)
        AnswerVoteFactory(answer=answer4, helpful=True)
        AnswerVoteFactory(answer=answer4, helpful=True)
        with self.subTest("limit to two most helpful answers"):
            self.assertEqual(set(question.helpful_replies), {answer3, answer4})
        question.solution = answer4
        question.save()
        with self.subTest("ignore the solution"):
            self.assertEqual(list(question.helpful_replies), [answer3])


class AddExistingTagTests(TestCase):
    """Tests for the add_existing_tag helper function."""

    def setUp(self):
        super().setUp()
        self.untagged_question = QuestionFactory()

    def test_tags_manager(self):
        """Make sure the TaggableManager exists.

        Full testing of functionality is a matter for taggit's tests.

        """
        tags_eq(self.untagged_question, [])

    def test_add_existing_case_insensitive(self):
        """Assert add_existing_tag works case-insensitively."""
        TagFactory(name="lemon", slug="lemon")
        add_existing_tag("LEMON", self.untagged_question.tags)
        tags_eq(self.untagged_question, ["lemon"])

    def test_add_existing_no_such_tag(self):
        """Assert add_existing_tag doesn't work when the tag doesn't exist."""
        with self.assertRaises(SumoTag.DoesNotExist):
            add_existing_tag("nonexistent tag", self.untagged_question.tags)


class OldQuestionsArchiveTest(ElasticTestCase):
    search_tests = True

    def test_archive_old_questions(self):
        last_updated = datetime.now() - timedelta(days=100)

        # created just now
        q1 = QuestionFactory()

        # created 200 days ago
        q2 = QuestionFactory(created=datetime.now() - timedelta(days=200), updated=last_updated)

        # created 200 days ago, already archived
        q3 = QuestionFactory(
            created=datetime.now() - timedelta(days=200),
            is_archived=True,
            updated=last_updated,
        )

        call_command("auto_archive_old_questions")

        # There are three questions.
        self.assertEqual(len(list(Question.objects.all())), 3)

        # q2 and q3 are now archived and updated times are the same
        archived_questions = list(Question.objects.filter(is_archived=True))
        self.assertEqual(
            sorted([(q.id, q.updated.date()) for q in archived_questions]),
            [(q.id, q.updated.date()) for q in [q2, q3]],
        )

        # q1 is still unarchived.
        archived_questions = list(Question.objects.filter(is_archived=False))
        self.assertEqual(sorted([q.id for q in archived_questions]), [q1.id])


class QuestionVisitsTests(TestCase):
    """Tests for the pageview statistics gathering."""

    # Need to monkeypatch close_old_connections out because it
    # does something screwy with the testing infra around transactions.
    @mock.patch.object(googleanalytics, "pageviews_by_question")
    def test_visit_count_from_analytics(self, pageviews_by_question):
        """Verify stored visit counts from mocked data."""
        q1 = QuestionFactory()
        q2 = QuestionFactory()
        q3 = QuestionFactory()

        pageviews_by_question.return_value = dict(
            row
            for row in (
                (q1.id, 42),
                (q2.id, 27),
                (q3.id, 1337),
                (123459, 3),
            )
        )

        QuestionVisits.reload_from_analytics()
        self.assertEqual(3, QuestionVisits.objects.count())
        self.assertEqual(42, QuestionVisits.objects.get(question_id=q1.id).visits)
        self.assertEqual(27, QuestionVisits.objects.get(question_id=q2.id).visits)
        self.assertEqual(1337, QuestionVisits.objects.get(question_id=q3.id).visits)

        # Change the data and run again to cover the update case.
        pageviews_by_question.return_value = dict(
            row
            for row in (
                (q1.id, 100),
                (q2.id, 200),
                (q3.id, 300),
            )
        )
        QuestionVisits.reload_from_analytics()
        self.assertEqual(3, QuestionVisits.objects.count())
        self.assertEqual(100, QuestionVisits.objects.get(question_id=q1.id).visits)
        self.assertEqual(200, QuestionVisits.objects.get(question_id=q2.id).visits)
        self.assertEqual(300, QuestionVisits.objects.get(question_id=q3.id).visits)


class QuestionVoteTests(TestCase):
    def test_add_metadata_over_1000_chars(self):
        qv = QuestionVoteFactory()
        qv.add_metadata("test1", "a" * 1001)
        metadata = VoteMetadata.objects.all()[0]
        self.assertEqual("a" * 1000, metadata.value)


class TestActions(TestCase):
    def test_question_create_action(self):
        """When a question is created, an Action is created too."""
        q = QuestionFactory()
        a = Action.objects.action_object(q).get()
        self.assertEqual(a.actor, q.creator)
        self.assertEqual(a.verb, "asked")
        self.assertEqual(a.target, None)

    def test_answer_create_action(self):
        """When an answer is created, an Action is created too."""
        q = QuestionFactory()
        ans = AnswerFactory(question=q)
        act = Action.objects.action_object(ans).get()
        self.assertEqual(act.actor, ans.creator)
        self.assertEqual(act.verb, "answered")
        self.assertEqual(act.target, q)

    def test_question_change_no_action(self):
        """When a question is changed, no Action should be created."""
        q = QuestionFactory()
        Action.objects.all().delete()
        q.save()  # trigger another post_save hook
        self.assertEqual(Action.objects.count(), 0)

    def test_answer_change_no_action(self):
        """When an answer is changed, no Action should be created."""
        q = QuestionFactory()
        Action.objects.all().delete()
        q.save()  # trigger another post_save hook
        self.assertEqual(Action.objects.count(), 0)

    def test_question_solved_makes_action(self):
        """When an answer is marked as the solution to a question, an Action should be created."""
        ans = AnswerFactory()
        Action.objects.all().delete()
        ans.question.set_solution(ans, ans.question.creator)

        act = Action.objects.action_object(ans).get()
        self.assertEqual(act.actor, ans.question.creator)
        self.assertEqual(act.verb, "marked as a solution")
        self.assertEqual(act.target, ans.question)

    @mock.patch.object(waffle, "switch_is_active")
    def test_create_question_creates_flag(self, switch_is_active):
        """Creating a question also creates a flag."""
        switch_is_active.side_effect = lambda name: name == "flagit-spam-autoflag"
        QuestionFactory(title="Test Question", content="Lorem Ipsum Dolor")
        self.assertEqual(1, FlaggedObject.objects.filter(reason="content_moderation").count())
