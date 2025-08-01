from datetime import date

import factory

from kitsune.dashboards.models import METRIC_CODE_CHOICES, WikiMetric


class WikiMetricFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WikiMetric

    code = METRIC_CODE_CHOICES[0][0]
    locale = "es"
    date = date.today()
    value = 42.0
