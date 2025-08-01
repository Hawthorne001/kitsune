import json
import logging
import re
from datetime import datetime, timedelta
from functools import cached_property
from urllib.parse import urlparse

import actstream
import actstream.actions
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import IntegrityError, models, transaction
from django.db.models import Count, Subquery
from django.db.models.functions import Now
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import is_valid_path
from django.utils import translation
from django.utils.translation import pgettext
from elasticsearch import ApiError, TransportError
from product_details import product_details

from kitsune.flagit.models import FlaggedObject
from kitsune.llm.tasks import question_classifier
from kitsune.products.models import Product, Topic
from kitsune.questions import config
from kitsune.questions.managers import AAQConfigManager, AnswerManager, QuestionManager
from kitsune.sumo.i18n import split_into_language_and_path
from kitsune.sumo.models import LocaleField, ModelBase
from kitsune.sumo.templatetags.jinja_helpers import urlparams, wiki_to_html
from kitsune.sumo.urlresolvers import reverse
from kitsune.sumo.utils import chunked
from kitsune.tags.models import BigVocabTaggableManager, SumoTag
from kitsune.upload.models import ImageAttachment
from kitsune.wiki.models import Document

log = logging.getLogger("k.questions")

VOTE_METADATA_MAX_LENGTH = 1000


class InvalidUserException(ValueError):
    pass


class AlreadyTakenException(Exception):
    pass


class VoteBase(ModelBase):
    created = models.DateTimeField(default=datetime.now, db_index=True)
    anonymous_id = models.CharField(max_length=40, db_index=True)

    class Meta:
        abstract = True

    def add_metadata(self, key, value):
        VoteMetadata.objects.create(vote=self, key=key, value=value[:VOTE_METADATA_MAX_LENGTH])


class AAQBase(ModelBase):
    created = models.DateTimeField(default=datetime.now, db_index=True)
    updated = models.DateTimeField(default=datetime.now, db_index=True)
    content = models.TextField()
    is_spam = models.BooleanField(default=False)
    marked_as_spam = models.DateTimeField(default=None, null=True)
    updated_column_name = "updated"

    class Meta:
        abstract = True

    def has_voted(self, request):
        """Is the user eligible to vote or
        did the user already vote for this answer or question?"""

        q_kwargs = {}

        if self.__class__ == Answer:
            VoteObject = AnswerVote
            q_kwargs.update({"answer": self})
        else:
            VoteObject = QuestionVote
            q_kwargs.update({"question": self})

        if request.user.is_authenticated:
            if self.creator == request.user:
                return True
            q_kwargs["creator"] = request.user
            return VoteObject.objects.filter(**q_kwargs).exists()
        elif request.anonymous.has_id:
            q_kwargs["anonymous_id"] = request.anonymous.anonymous_id
            return VoteObject.objects.filter(**q_kwargs).exists()
        else:
            return False

    def clear_cached_html(self):
        cache.delete(self.html_cache_key % self.id)

    def clear_cached_images(self):
        cache.delete(self.images_cache_key % self.id)


class Question(AAQBase):
    """A support question."""

    title = models.CharField(max_length=255)
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name="questions")

    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="questions_updated"
    )
    last_answer = models.ForeignKey(
        "Answer", on_delete=models.SET_NULL, related_name="last_reply_in", null=True, blank=True
    )
    num_answers = models.IntegerField(default=0, db_index=True)
    solution = models.ForeignKey(
        "Answer", on_delete=models.SET_NULL, related_name="solution_for", null=True
    )
    is_locked = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False, null=True)
    num_votes_past_week = models.PositiveIntegerField(default=0, db_index=True)

    marked_as_spam_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="questions_marked_as_spam"
    )

    images = GenericRelation(ImageAttachment)
    flags = GenericRelation(FlaggedObject)

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, null=True, default=None, related_name="questions"
    )
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, null=True, related_name="questions")

    locale = LocaleField(default=settings.WIKI_DEFAULT_LANGUAGE)

    taken_by = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True)
    taken_until = models.DateTimeField(blank=True, null=True)

    tags = BigVocabTaggableManager(related_name="questions")

    html_cache_key = "question:html:%s"
    tags_cache_key = "question:tags:%s"
    images_cache_key = "question:images:%s"
    contributors_cache_key = "question:contributors:%s"
    moderation_timestamp = models.DateTimeField(default=None, null=True)

    update_topic_counter = models.IntegerField(default=0)

    objects = QuestionManager()

    class Meta:
        ordering = ["-updated"]
        permissions = (
            ("tag_question", "Can add tags to and remove tags from questions"),
            ("change_solution", "Can change/remove the solution to a question"),
        )

    def __str__(self):
        return self.title

    def set_needs_info(self):
        """Mark question as NEEDS_INFO."""
        self.tags.add(config.NEEDS_INFO_TAG_NAME)
        self.clear_cached_tags()

    def unset_needs_info(self):
        """Remove NEEDS_INFO."""
        self.tags.remove(config.NEEDS_INFO_TAG_NAME)
        self.clear_cached_tags()

    @property
    def needs_info(self):
        return self.tags.filter(slug=config.NEEDS_INFO_TAG_NAME).count() > 0

    @property
    def content_parsed(self):
        return _content_parsed(self, self.locale)

    def clear_cached_tags(self):
        cache.delete(self.tags_cache_key % self.id)

    def clear_cached_contributors(self):
        cache.delete(self.contributors_cache_key % self.id)

    def save(self, update=False, *args, **kwargs):
        """Override save method to take care of updated if requested."""

        new = not self.id

        if not new:
            self.clear_cached_html()
            if update:
                self.updated = datetime.now()

        super().save(*args, **kwargs)

        if new:
            # actstream
            # Authors should automatically follow their own questions.
            actstream.actions.follow(self.creator, self, send_action=False, actor_only=False)
            # Either automatically classify the question or add it to the moderation queue
            question_classifier.delay(self.id)

    def add_metadata(self, **kwargs):
        """Add (save to db) the passed in metadata.

        Usage:
        question = Question.objects.get(pk=1)
        question.add_metadata(ff_version='3.6.3', os='Linux')

        """
        for key, value in list(kwargs.items()):
            QuestionMetaData.objects.create(question=self, name=key, value=value)
        self._metadata = None

    def clear_mutable_metadata(self):
        """Clear the mutable metadata.

        This excludes immutable fields: user agent, product, and category.

        """
        self.metadata_set.exclude(
            name__in=["useragent", "product", "category", "kb_visits_prior"]
        ).delete()
        self._metadata = None

    def remove_metadata(self, name):
        """Delete the specified metadata."""
        self.metadata_set.filter(name=name).delete()
        self._metadata = None

    @property
    def metadata(self):
        """Dictionary access to metadata

        Caches the full metadata dict after first call.

        """
        if not hasattr(self, "_metadata") or self._metadata is None:
            self._metadata = {m.name: m.value for m in self.metadata_set.all()}
        return self._metadata

    @property
    def solver(self):
        """Get the user that solved the question."""
        solver_id = self.metadata.get("solver_id")
        if solver_id:
            try:
                return User.objects.get(id=solver_id)
            except User.DoesNotExist:
                return None

    @property
    def product_config(self):
        """Return the product config this question is about or None"""
        try:
            aaq_config = AAQConfig.objects.get(is_active=True, product=self.product)
        except AAQConfig.DoesNotExist:
            return None
        else:
            return aaq_config

    @property
    def product_slug(self):
        """Return the product slug for this question.

        It returns 'all' in the off chance that there are no products."""
        if not hasattr(self, "_product_slug") or self._product_slug is None:
            self._product_slug = self.product.slug if self.product else None

        return self._product_slug

    def handle_metadata_tags(self, action):
        """
        Add or remove tags that are implied by my metadata.
        You don't need to call save on the question after this.
        """
        tags = []

        if product_config := self.product_config:
            for tag in product_config.associated_tags.all():
                tags.append(tag)

        version = self.metadata.get("ff_version", "")

        # Remove the beta (b*), aurora (a2) or nightly (a1) suffix.
        version = re.split("[a-b]", version)[0]

        dev_releases = product_details.firefox_history_development_releases

        if (
            version in dev_releases
            or version in product_details.firefox_history_stability_releases
            or version in product_details.firefox_history_major_releases
        ):
            tags.append("Firefox {}".format(version))
            tenths = _tenths_version(version)
            if tenths:
                tags.append("Firefox {}".format(tenths))
        elif _has_beta(version, dev_releases):
            tags.append("Firefox {}".format(version))
            tags.append("beta")

        # Add a tag for the OS but only if it already exists as a non-segmentation tag.
        if os := self.metadata.get("os"):
            try:
                os_tag = SumoTag.objects.non_segmentation_tags().filter(name__iexact=os).get()
            except SumoTag.DoesNotExist:
                pass
            else:
                tags.append(os_tag)

        product_md = self.metadata.get("product")
        topic_md = self.metadata.get("category")
        if self.product and not product_md:
            tags.append(self.product.slug)
        if self.topic and not topic_md:
            tags.append(self.topic.slug)

        getattr(self.tags, action)(*tags)

    def auto_tag(self):
        """
        Add tags that are implied by my metadata.
        """
        self.handle_metadata_tags("add")

    def remove_auto_tags(self):
        """
        Remove tags that are implied by my metadata.
        """
        self.handle_metadata_tags("remove")

    def get_absolute_url(self):
        # Note: If this function changes, we need to change it in
        # extract_document, too.
        return reverse("questions.details", kwargs={"question_id": self.id})

    @property
    def num_votes(self):
        """Get the number of votes for this question."""
        if not hasattr(self, "_num_votes"):
            n = QuestionVote.objects.filter(question=self).count()
            self._num_votes = n
        return self._num_votes

    def sync_num_votes_past_week(self):
        """Get the number of votes for this question in the past week."""
        last_week = datetime.now().date() - timedelta(days=7)
        # Use "__range" to ensure the database index is used in Postgres.
        n = QuestionVote.objects.filter(question=self, created__range=(last_week, Now())).count()
        self.num_votes_past_week = n
        return n

    @property
    def helpful_replies(self):
        """Return answers that have been voted as helpful."""

        helpful_ids = list(
            AnswerVote.objects.filter(helpful=True, answer__question=self)
            .order_by()
            .values("answer")
            .annotate(score=Count("*"))
            .filter(score__gt=0)
            .order_by("-score")
            .values_list("answer", flat=True)[:2]
        )

        # Exclude the solution if it is set
        if self.solution and self.solution.id in helpful_ids:
            helpful_ids.remove(self.solution.id)

        if len(helpful_ids) > 0:
            return self.answers.filter(id__in=helpful_ids)
        else:
            return []

    def is_contributor(self, user):
        """Did the passed in user contribute to this question?"""
        if user.is_authenticated:
            return user.id in self.contributors

        return False

    @property
    def contributors(self):
        """The contributors to the question."""
        cache_key = self.contributors_cache_key % self.id
        contributors = cache.get(cache_key)
        if contributors is None:
            contributors = self.answers.all().values_list("creator_id", flat=True)
            contributors = list(contributors)
            contributors.append(self.creator_id)
            cache.add(cache_key, contributors, settings.CACHE_MEDIUM_TIMEOUT)
        return contributors

    @property
    def is_solved(self):
        return self.solution_id is not None

    @property
    def is_offtopic(self):
        return config.OFFTOPIC_TAG_NAME in [t.name for t in self.my_tags]

    @cached_property
    def created_after_failed_kb_deflection(self) -> bool:
        """
        Returns a boolean indicating whether or not this question was created after its
        creator had visited one or more KB articles with the same product and topic.
        """
        return self.metadata_set.filter(name="kb_visits_prior").exists()

    @cached_property
    def kb_visits_prior_to_creation(self) -> list[str]:
        """
        Returns the list of KB article URL's visited prior to the creation of this question.
        """
        try:
            metadata = self.metadata_set.filter(name="kb_visits_prior").get()
        except QuestionMetaData.DoesNotExist:
            return []
        return json.loads(metadata.value)

    @property
    def my_tags(self):
        """A caching wrapper around self.tags.all()."""
        cache_key = self.tags_cache_key % self.id
        tags = cache.get(cache_key)
        if tags is None:
            tags = list(self.tags.all().order_by("name"))
            cache.add(cache_key, tags, settings.CACHE_MEDIUM_TIMEOUT)
        return tags

    @classmethod
    def get_serializer(cls, serializer_type="full"):
        # Avoid circular import
        from kitsune.questions import api

        if serializer_type == "full":
            return api.QuestionSerializer
        elif serializer_type == "fk":
            return api.QuestionFKSerializer
        else:
            raise ValueError('Unknown serializer type "{}".'.format(serializer_type))

    @classmethod
    def recent_asked_count(cls, extra_filter=None):
        """Returns the number of questions asked in the last 24 hours."""
        start = datetime.now() - timedelta(hours=24)
        # Use "__range" to ensure the database index is used in Postgres.
        qs = cls.objects.filter(created__range=(start, Now()), creator__is_active=True)
        if extra_filter:
            qs = qs.filter(extra_filter)
        return qs.count()

    @classmethod
    def recent_unanswered_count(cls, extra_filter=None):
        """Returns the number of questions that have not been answered in the
        last 24 hours.
        """
        # Use "__range" to ensure the database index is used in Postgres.
        start = datetime.now() - timedelta(hours=24)
        qs = cls.objects.filter(
            num_answers=0,
            created__range=(start, Now()),
            is_spam=False,
            is_locked=False,
            is_archived=False,
            creator__is_active=1,
        )
        if extra_filter:
            qs = qs.filter(extra_filter)
        return qs.count()

    @classmethod
    def from_url(cls, url, id_only=False):
        """Returns the question that the URL represents.

        If the question doesn't exist or the URL isn't a question URL,
        this returns None.

        If id_only is requested, we just return the question id and
        we don't validate the existence of the question (this saves us
        from making a million or so db calls).
        """
        parsed = urlparse(url)
        language, _ = split_into_language_and_path(parsed.path)

        with translation.override(language):
            match = is_valid_path(parsed.path)

        if not (match and match.url_name == "questions.details"):
            return None

        question_id = int(match.captured_kwargs["question_id"])

        if id_only:
            return question_id

        try:
            question = cls.objects.get(id=question_id)
        except cls.DoesNotExist:
            return None

        return question

    @property
    def num_visits(self):
        """Get the number of visits for this question."""
        if not hasattr(self, "_num_visits"):
            try:
                self._num_visits = QuestionVisits.objects.get(question=self).visits
            except QuestionVisits.DoesNotExist:
                self._num_visits = None

        return self._num_visits

    @property
    def editable(self):
        return not self.is_locked and not self.is_archived

    @property
    def age(self):
        """The age of the question, in seconds."""
        delta = datetime.now() - self.created
        return delta.seconds + delta.days * 24 * 60 * 60

    def set_solution(self, answer, solver):
        """
        Sets the solution, and fires any needed events.

        Does not check permission of the user making the change.
        """
        # Avoid circular import
        from kitsune.questions.events import QuestionSolvedEvent

        self.solution = answer
        self.save()
        self.add_metadata(solver_id=str(solver.id))
        QuestionSolvedEvent(answer).fire(exclude=[self.creator])
        actstream.action.send(
            solver, verb="marked as a solution", action_object=answer, target=self
        )

    @property
    def _content_for_related(self):
        """Text to use in elastic more_like_this query."""
        content = [self.title, self.content]
        if self.topic:
            with translation.override(self.locale):
                # use the question's locale, rather than the user's
                content += [pgettext("DB: products.Topic.title", self.topic.title)]

        return content

    @property
    def related_documents(self):
        """Return documents that are 'morelikethis' one"""
        if not self.product:
            return []

        # First try to get the results from the cache
        key = "questions_question:related_docs:{}".format(self.id)
        documents = cache.get(key)
        if documents is not None:
            log.debug(
                "Getting MLT documents for {question} from cache.".format(question=repr(self))
            )
            return documents

        # avoid circular import issue
        from kitsune.search.documents import WikiDocument

        try:
            search = (
                WikiDocument.search()
                .filter("term", product_ids=self.product.id)
                .query(
                    "more_like_this",
                    fields=[
                        f"title.{self.locale}",
                        f"content.{self.locale}",
                        f"summary.{self.locale}",
                        f"keywords.{self.locale}",
                    ],
                    like=self._content_for_related,
                    max_query_terms=15,
                )
                .source([f"slug.{self.locale}", f"title.{self.locale}"])
            )
            documents = [
                {
                    "url": reverse(
                        "wiki.document", args=[hit.slug[self.locale]], locale=self.locale
                    ),
                    "title": hit.title[self.locale],
                }
                for hit in search[:3].execute().hits
            ]
            cache.set(key, documents, settings.CACHE_LONG_TIMEOUT)
        except (ApiError, TransportError):
            log.exception("ES MLT related_documents")
            documents = []

        return documents

    @property
    def related_questions(self):
        """Return questions that are 'morelikethis' one"""
        if not self.product:
            return []

        # First try to get the results from the cache
        key = "questions_question:related_questions:{}".format(self.id)
        questions = cache.get(key)
        if questions is not None:
            log.debug(
                "Getting MLT questions for {question} from cache.".format(question=repr(self))
            )
            return questions

        # avoid circular import issue
        from kitsune.search.documents import QuestionDocument

        try:
            search = (
                QuestionDocument.search()
                .filter("term", question_product_id=self.product.id)
                .exclude("exists", field="updated")
                .exclude("term", _id=self.id)
                .query(
                    "more_like_this",
                    fields=[f"question_title.{self.locale}", f"question_content.{self.locale}"],
                    like=self._content_for_related,
                    max_query_terms=15,
                )
                .source(["question_id", "question_title"])
            )
            questions = [
                {
                    "url": reverse("questions.details", kwargs={"question_id": hit.question_id}),
                    "title": hit.question_title[self.locale],
                }
                for hit in search[:3].execute().hits
            ]
            cache.set(key, questions, settings.CACHE_LONG_TIMEOUT)
        except (ApiError, TransportError):
            log.exception("ES MLT related_questions")
            questions = []

        return questions

    # Permissions

    def allows_edit(self, user):
        """Return whether `user` can edit this question."""
        return user.has_perm("questions.change_question") or (
            self.editable and self.creator == user
        )

    def allows_delete(self, user):
        """Return whether `user` can delete this question."""
        return user.has_perm("questions.delete_question")

    def allows_lock(self, user):
        """Return whether `user` can lock this question."""
        return user.has_perm("questions.lock_question")

    def allows_archive(self, user):
        """Return whether `user` can archive this question."""
        return user.has_perm("questions.archive_question")

    def allows_new_answer(self, user):
        """Return whether `user` can answer (reply to) this question."""
        return user.has_perm("questions.add_answer") or (self.editable and user.is_authenticated)

    def allows_solve(self, user):
        """Return whether `user` can select the solution to this question."""
        return self.editable and (
            user == self.creator or user.has_perm("questions.change_solution")
        )

    def allows_unsolve(self, user):
        """Return whether `user` can unsolve this question."""
        return self.editable and (
            user == self.creator or user.has_perm("questions.change_solution")
        )

    def allows_flag(self, user):
        """Return whether `user` can flag this question."""
        return user.is_authenticated and user != self.creator and self.editable

    def mark_as_spam(self, by_user):
        """Mark the question as spam by the specified user."""
        self.is_spam = True
        self.marked_as_spam = datetime.now()
        self.marked_as_spam_by = by_user
        self.save()

    @property
    def is_taken(self):
        """
        Convenience method to check that a question is taken.

        If the question is no longer validly taken (due to missing user or expired time),
        this will reset the database fields and return False.
        """
        if self.taken_by is None or self.taken_until is None or self.taken_until < datetime.now():
            if (self.taken_by is not None) or (self.taken_until is not None):
                self.taken_by = None
                self.taken_until = None
                self.save()
            return False
        return True

    def take(self, user, force=False):
        """
        Sets the user that is currently working on this question.

        May raise InvalidUserException if the user is not permitted to take
        the question (such as if the question is owned by the user).

        May raise AlreadyTakenException if the question is already taken
        by a different user, and the force paramater is not True.

        If the user is the same as the user that currently has the question,
        the timer will be updated   .
        """

        if user == self.creator:
            raise InvalidUserException

        if self.taken_by not in [None, user] and not force:
            raise AlreadyTakenException

        self.taken_by = user
        self.taken_until = datetime.now() + timedelta(seconds=config.TAKE_TIMEOUT)
        self.save()

    def get_images(self):
        """A cached version of self.images.all()."""
        cache_key = self.images_cache_key % self.id
        images = cache.get(cache_key)
        if images is None:
            images = list(self.images.all())
            cache.add(cache_key, images, settings.CACHE_MEDIUM_TIMEOUT)
        return images


class QuestionMetaData(ModelBase):
    """Metadata associated with a support question."""

    question = models.ForeignKey("Question", on_delete=models.CASCADE, related_name="metadata_set")
    name = models.SlugField(db_index=True)
    value = models.TextField()

    class Meta:
        unique_together = ("question", "name")

    def __str__(self):
        return "{}: {}".format(self.name, self.value[:50])


class QuestionVisits(ModelBase):
    """Web stats for questions."""

    question = models.ForeignKey(Question, on_delete=models.CASCADE, unique=True)
    visits = models.IntegerField(db_index=True)

    @classmethod
    def reload_from_analytics(cls, verbose=False):
        """Update the stats from Google Analytics."""
        from kitsune.sumo import googleanalytics

        with transaction.atomic():
            if verbose:
                log.info("Gathering pageviews per question from GA4 data API...")

            pageviews_by_question_id = googleanalytics.pageviews_by_question(verbose=verbose)

            total_count = len(pageviews_by_question_id)

            if verbose:
                log.info(f"Gathered pageviews for {total_count} questions.")

            def create_batch(batch_of_question_ids):
                """
                Create a batch of instances in one shot, but only include instances that
                refer to an existing Question, so we avoid triggering an integrity error.
                A call to this function makes only two databases queries no matter how
                many instances we need to validate and create.
                """
                cls.objects.bulk_create(
                    [
                        instance_by_question_id[id]
                        for id in Question.objects.filter(
                            id__in=batch_of_question_ids
                        ).values_list("id", flat=True)
                    ]
                )

            instance_by_question_id = {}

            for i, (question_id, visits) in enumerate(pageviews_by_question_id.items(), start=1):
                instance_by_question_id[question_id] = cls(
                    question_id=Subquery(Question.objects.filter(id=question_id).values("id")),
                    visits=visits,
                )

                # Update the question visits in batches of 30K to avoid memory issues.
                if ((i % 30000) != 0) and (i != total_count):
                    continue

                # We've got a batch, so let's update them.

                question_ids = list(instance_by_question_id)

                # Next, let's clear out the stale instances that have new results.
                if verbose:
                    log.info(f"Deleting {len(question_ids)} stale instances of {cls.__name__}...")

                cls.objects.filter(question_id__in=question_ids).delete()

                # Then we can create fresh instances for the questions that have results.
                if verbose:
                    log.info(f"Creating {len(question_ids)} fresh instances of {cls.__name__}...")

                # Let's create the fresh instances in batches of 1K, so we avoid exposing
                # ourselves to the possibility of transgressing some query size limit.
                for batch_of_question_ids in chunked(question_ids, 1000):
                    if verbose:
                        log.info(f"Creating a batch of {len(batch_of_question_ids)} instances...")

                    try:
                        with transaction.atomic():
                            create_batch(batch_of_question_ids)
                    except IntegrityError:
                        # There is a very slim chance that one or more Questions have been
                        # deleted in the moment of time between the formation of the list
                        # of valid instances and actually creating them, so let's give it
                        # one more try, assuming there's an even slimmer chance that
                        # lightning will strike twice. If this one fails, we'll roll-back
                        # everything and give up on the entire effort.
                        create_batch(batch_of_question_ids)

                # We're done with this batch, so let's clear the memory for the next one.
                instance_by_question_id.clear()

        if verbose:
            log.info("Done.")


class QuestionLocale(ModelBase):
    locale = LocaleField(choices=settings.LANGUAGE_CHOICES_ENGLISH, unique=True)

    class Meta:
        verbose_name = "AAQ enabled locale"

    def __str__(self) -> str:
        return self.locale


class AAQConfig(ModelBase):
    title = models.CharField(max_length=255, default="")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="aaq_configs")
    pinned_articles = models.ManyToManyField(Document, null=True, blank=True)
    associated_tags = models.ManyToManyField(SumoTag, null=True, blank=True)
    enabled_locales = models.ManyToManyField(QuestionLocale)
    # Whether the configuration is active or not. Only one can be active per product
    is_active = models.BooleanField(default=False)
    extra_fields = models.JSONField(default=list, blank=True)

    objects = AAQConfigManager()

    class Meta:
        verbose_name = "AAQ configuration"
        constraints = [
            models.UniqueConstraint(fields=["product", "is_active"], name="unique_active_config")
        ]

    def __str__(self):
        return f"{self.product} Configuration"


class Answer(AAQBase):
    """An answer to a support question."""

    question = models.ForeignKey("Question", on_delete=models.CASCADE, related_name="answers")
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name="answers")
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="answers_updated"
    )
    page = models.IntegerField(default=1)
    marked_as_spam_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="answers_marked_as_spam"
    )

    images = GenericRelation(ImageAttachment)
    flags = GenericRelation(FlaggedObject)

    html_cache_key = "answer:html:%s"
    images_cache_key = "answer:images:%s"

    objects = AnswerManager()

    class Meta:
        ordering = ["created"]
        permissions = (("bypass_answer_ratelimit", "Can bypass answering ratelimit"),)

    def __str__(self):
        return "{}: {}".format(self.question.title, self.content[:50])

    @property
    def content_parsed(self):
        return _content_parsed(self, self.question.locale)

    def save(self, update=True, no_notify=False, *args, **kwargs):
        """
        Override save method to update question info and take care of
        updated.
        """

        new = self.id is None

        if new:
            page = self.question.num_answers // config.ANSWERS_PER_PAGE + 1
            self.page = page
        else:
            self.updated = datetime.now()
            self.clear_cached_html()

        super().save(*args, **kwargs)

        self.question.num_answers = Answer.objects.filter(
            question=self.question, is_spam=False
        ).count()
        latest = Answer.objects.filter(question=self.question, is_spam=False).order_by("-created")[
            :1
        ]
        self.question.last_answer = self if new else latest[0] if len(latest) else None
        self.question.save(update)

        if new:
            # Occasionally, num_answers seems to get out of sync with the
            # actual number of answers. This changes it to pull from
            # uncached on the off chance that fixes it. Plus if we enable
            # caching of counts, this will continue to work.
            self.question.clear_cached_contributors()

            if not no_notify:
                if not self.is_spam:
                    # Avoid circular import
                    from kitsune.questions.events import QuestionReplyEvent

                    QuestionReplyEvent(self).fire(exclude=[self.creator])

                # actstream
                actstream.actions.follow(self.creator, self, send_action=False, actor_only=False)
                actstream.actions.follow(
                    self.creator, self.question, send_action=False, actor_only=False
                )

    def delete(self, *args, **kwargs):
        """Override delete method to update parent question info."""
        from kitsune.questions.tasks import update_answer_pages

        question = Question.objects.get(pk=self.question.id)
        if question.last_answer == self:
            answers = question.answers.all().order_by("-created")
            try:
                question.last_answer = answers[1]
            except IndexError:
                # The question has only one answer
                question.last_answer = None
        if question.solution == self:
            question.solution = None

        answers = question.answers.filter(is_spam=False)
        question.num_answers = answers.count() - 1
        question.save()

        super().delete(*args, **kwargs)
        question.clear_cached_contributors()

        update_answer_pages.delay(question.id)

    def get_solution_url(self, watch):
        url = reverse(
            "questions.solve",
            kwargs={"question_id": self.question_id, "answer_id": self.id},
        )
        return urlparams(url, watch=watch.secret)

    def get_absolute_url(self):
        query = {}
        if self.page > 1:
            query = {"page": self.page}

        url = reverse("questions.details", kwargs={"question_id": self.question_id})
        return urlparams(url, hash="answer-{}".format(self.id), **query)

    @property
    def num_votes(self):
        """Get the total number of votes for this answer."""
        return AnswerVote.objects.filter(answer=self).count()

    @property
    def num_helpful_votes(self):
        """Get the number of helpful votes for this answer."""
        return AnswerVote.objects.filter(answer=self, helpful=True).count()

    @property
    def num_unhelpful_votes(self):
        """Get the number of unhelpful votes for this answer."""
        return AnswerVote.objects.filter(answer=self, helpful=False).count()

    @property
    def creator_num_answers(self):
        # Avoid circular import, utils.py imports Question
        from kitsune.questions.utils import num_answers

        return num_answers(self.creator)

    @property
    def creator_num_solutions(self):
        # Avoid circular import, utils.py imports Question
        from kitsune.questions.utils import num_solutions

        return num_solutions(self.creator)

    @classmethod
    def last_activity_for(cls, user):
        """Returns the datetime of the user's last answer."""
        try:
            return (
                Answer.objects.filter(creator=user)
                .order_by("-created")
                .values_list("created", flat=True)[0]
            )
        except IndexError:
            return None

    def allows_edit(self, user, question=None):
        """Return whether `user` can edit this answer."""
        if question is None:
            question = self.question

        return user.has_perm("questions.change_answer") or (
            question.editable and self.creator == user
        )

    def allows_delete(self, user):
        """Return whether `user` can delete this answer."""
        return user.has_perm("questions.delete_answer")

    def allows_flag(self, user, question=None):
        """Return whether `user` can flag this answer."""
        if question is None:
            question = self.question

        return user.is_authenticated and user != self.creator and question.editable

    def get_images(self):
        """A cached version of self.images.all().

        Because django-cache-machine doesn't cache empty lists.
        """
        cache_key = self.images_cache_key % self.id
        images = cache.get(cache_key)
        if images is None:
            images = list(self.images.all())
            cache.add(cache_key, images, settings.CACHE_MEDIUM_TIMEOUT)
        return images

    @classmethod
    def get_serializer(cls, serializer_type="full"):
        # Avoid circular import
        from kitsune.questions import api

        if serializer_type == "full":
            return api.AnswerSerializer
        elif serializer_type == "fk":
            return api.AnswerFKSerializer
        else:
            raise ValueError('Unknown serializer type "{}".'.format(serializer_type))

    def mark_as_spam(self, by_user):
        """Mark the answer as spam by the specified user."""
        self.is_spam = True
        self.marked_as_spam = datetime.now()
        self.marked_as_spam_by = by_user
        self.save()


class QuestionVote(VoteBase):
    """I have this problem too.
    Keeps track of users that have problem over time."""

    question = models.ForeignKey("Question", on_delete=models.CASCADE, related_name="votes")
    creator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="question_votes", null=True
    )


class AnswerVote(VoteBase):
    """Helpful or Not Helpful vote on Answer."""

    answer = models.ForeignKey("Answer", on_delete=models.CASCADE, related_name="votes")
    helpful = models.BooleanField(default=False)
    creator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="answer_votes", null=True
    )


class VoteMetadata(ModelBase):
    """Metadata for question and answer votes."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    vote = GenericForeignKey()
    key = models.CharField(max_length=40, db_index=True)
    value = models.CharField(max_length=VOTE_METADATA_MAX_LENGTH)


def send_vote_update_task(**kwargs):
    from kitsune.questions.tasks import update_question_votes

    if kwargs.get("created"):
        q = kwargs.get("instance").question
        update_question_votes.delay(q.id)


post_save.connect(send_vote_update_task, sender=QuestionVote)


_tenths_version_pattern = re.compile(r"(\d+\.\d+).*")


def _tenths_version(full_version):
    """Return the major and minor version numbers from a full version string.

    Don't return bugfix version, beta status, or anything futher. If there is
    no major or minor version in the string, return ''.

    """
    match = _tenths_version_pattern.match(full_version)
    if match:
        return match.group(1)
    return ""


def _has_beta(version, dev_releases):
    """Returns True if the version has a beta release.

    For example, if:
        dev_releases={...u'4.0rc2': u'2011-03-18',
                      u'5.0b1': u'2011-05-20',
                      u'5.0b2': u'2011-05-20',
                      u'5.0b3': u'2011-06-01'}
    and you pass '5.0', it return True since there are 5.0 betas in the
    dev_releases dict. If you pass '6.0', it returns False.
    """
    return version in [re.search(r"(\d+\.)+\d+", s).group(0) for s in list(dev_releases.keys())]


def _content_parsed(obj, locale):
    cache_key = obj.html_cache_key % obj.id
    html = cache.get(cache_key)
    if html is None:
        html = wiki_to_html(obj.content, locale)
        cache.add(cache_key, html, settings.CACHE_MEDIUM_TIMEOUT)
    return html


@receiver(post_save, sender=Question, dispatch_uid="question_create_actionstream")
def add_action_for_new_question(sender, instance, created, **kwargs):
    if created:
        actstream.action.send(instance.creator, verb="asked", action_object=instance)


@receiver(post_save, sender=Answer, dispatch_uid="answer_create_actionstream")
def add_action_for_new_answer(sender, instance, created, **kwargs):
    if created:
        actstream.action.send(
            instance.creator,
            verb="answered",
            action_object=instance,
            target=instance.question,
        )
