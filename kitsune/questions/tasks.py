import logging
from datetime import date, datetime, timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce, Now
from sentry_sdk import capture_exception

from kitsune.community.utils import num_deleted_contributions
from kitsune.kbadge.utils import get_or_create_badge
from kitsune.questions.config import ANSWERS_PER_PAGE

log = logging.getLogger("k.task")


@shared_task(rate_limit="1/s")
def update_question_votes(question_id):
    from kitsune.questions.models import Question

    log.debug("Got a new QuestionVote for question_id={}.".format(question_id))

    try:
        q = Question.objects.get(id=question_id)
        q.sync_num_votes_past_week()
        q.save(force_update=True)
    except Question.DoesNotExist:
        log.info("Question id={} deleted before task.".format(question_id))


@shared_task(rate_limit="4/s")
def update_question_vote_chunk(question_ids):
    """Given a list of questions, update the "num_votes_past_week" attribute of each one."""
    from kitsune.questions.models import Question, QuestionVote

    log.info("Calculating past week votes for {} questions.".format(len(question_ids)))

    past_week = (datetime.now() - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    Question.objects.filter(id__in=question_ids).update(
        num_votes_past_week=Coalesce(
            Subquery(
                # Use "__range" to ensure the database index is used in Postgres.
                QuestionVote.objects.filter(
                    question_id=OuterRef("id"), created__range=(past_week, Now())
                )
                .order_by()
                .values("question_id")
                .annotate(count=Count("*"))
                .values("count")
            ),
            0,
        )
    )


@shared_task(rate_limit="4/m")
def update_answer_pages(question_id: int):
    from kitsune.questions.models import Question

    try:
        question = Question.objects.get(id=question_id)
    except Question.DoesNotExist as err:
        capture_exception(err)
        return

    log.debug(
        "Recalculating answer page numbers for question {}: {}".format(question.pk, question.title)
    )

    i = 0
    answers = question.answers.using("default").order_by("created")
    for answer in answers.filter(is_spam=False):
        answer.page = i // ANSWERS_PER_PAGE + 1
        answer.save(no_notify=True)
        i += 1


@shared_task
def maybe_award_badge(badge_template: dict, year: int, user_id: int) -> bool:
    """Award the specific badge to the user if they've earned it."""
    badge = get_or_create_badge(badge_template, year)

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist as err:
        capture_exception(err)
        return False

    # If the user already has the badge, there is nothing else to do.
    if badge.is_awarded_to(user):
        return False

    # Count the number of replies tweeted in the current year.
    from kitsune.questions.models import Answer

    num_contributions = Answer.objects.filter(
        creator=user, created__gte=date(year, 1, 1), created__lt=date(year + 1, 1, 1)
    ).count() + num_deleted_contributions(
        Answer,
        contributor=user,
        contribution_timestamp__gte=date(year, 1, 1),
        contribution_timestamp__lt=date(year + 1, 1, 1),
    )

    # If the count is at or above the limit, award the badge.
    if num_contributions >= settings.BADGE_LIMIT_SUPPORT_FORUM:
        badge.award_to(user)
        return True

    return False


@shared_task
def cleanup_old_spam():
    """Clean up spam Questions and Answers older than the configured cutoff period."""
    from kitsune.questions.handlers import OldSpamCleanupHandler

    log.info("Starting cleanup of old spam content.")
    handler = OldSpamCleanupHandler()

    try:
        result = handler.cleanup_old_spam()
    except Exception as err:
        capture_exception(err)
    else:
        log.info(
            "Spam cleanup completed: deleted %d questions and %d answers marked as spam before %s",
            result["questions_deleted"],
            result["answers_deleted"],
            result["cutoff_date"],
        )

        return result
