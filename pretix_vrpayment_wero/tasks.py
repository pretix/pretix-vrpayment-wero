from typing import Callable, Union

import logging
from datetime import timedelta
from django.db import transaction
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment, OrderRefund
from pretix.base.payment import PaymentException
from pretix.base.services.tasks import TransactionAwareTask
from pretix.celery_app import app
from pretix.helpers import OF_SELF
from rest_framework.exceptions import ValidationError

from pretix_vrpayment_wero.client import (
    GeneralError,
    WeroApiError,
)
from pretix_vrpayment_wero.info_data import (
    load_payment_info_data,
    load_refund_info_data,
    payment_info,
    refund_info,
)

logger = logging.getLogger(__name__)


@app.task(base=TransactionAwareTask)
@scopes_disabled()
@transaction.atomic
def refresh_transaction(payment: int, trigger_polling: bool = True):
    payment = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment)
    pprov = payment.payment_provider

    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        return None

    try:
        load_payment_info_data(payment)
    except ValidationError as e:
        payment.fail(log_data=str(e))
        return None

    with payment_info(payment, save=True) as info_data:
        info_data.last_polling = now()

    try:
        consent = pprov.get_consent(payment)
        if consent is None:
            _poll_task(payment, refresh_transaction, trigger_polling)
            return None

        authorizations = pprov.get_authorizations(payment)
        if authorizations is None:
            _poll_task(payment, refresh_transaction, trigger_polling)
            return None

        payment.refresh_from_db()

        if payment.info_data.get("captures", None) is None:
            pprov.capture(payment)

        captures = pprov.get_captures(payment, authorizations)
        if captures is None:
            _poll_task(payment, refresh_transaction, trigger_polling)
            return None

    except GeneralError as e:
        _poll_task(payment, refresh_transaction, trigger_polling)
        logger.exception(e)
    except (PaymentException, WeroApiError) as e:
        logger.exception(e)


@app.task(base=TransactionAwareTask)
@scopes_disabled()
@transaction.atomic
def refresh_refund(refund: int, trigger_polling: bool = True):
    refund = OrderRefund.objects.select_for_update(of=OF_SELF).get(pk=refund)
    pprov = refund.payment.payment_provider

    if refund.state != OrderRefund.REFUND_STATE_TRANSIT:
        return None

    try:
        load_refund_info_data(refund)
    except ValidationError as e:
        pprov._fail_refund(refund, e)
        return None

    with refund_info(refund, save=True) as info_data:
        info_data.last_polling = now()

    try:
        pprov.get_refunds(refund)
        refund.refresh_from_db()

        if refund.state == OrderRefund.REFUND_STATE_TRANSIT:
            _poll_task(refund, refresh_refund, trigger_polling)
    except GeneralError as e:
        logger.exception(e)
        _poll_task(refund, refresh_refund, trigger_polling)
    except WeroApiError as e:
        logger.exception(e.wero_error.message)
        if e.retry:
            _poll_task(refund, refresh_refund, trigger_polling)
        else:
            pprov._fail_refund(refund, e.wero_error.message)
    except PaymentException as e:
        logger.exception(e)


def _poll_task(
    obj: Union[OrderPayment, OrderRefund], task: Callable, trigger_polling: bool
):
    if not trigger_polling:
        return

    payment_age = now() - obj.created

    match payment_age:
        case age if age < timedelta(minutes=5):
            countdown = 10
        case age if age < timedelta(minutes=15):
            countdown = 30
        case age if age < timedelta(minutes=30):
            countdown = 60
        case _:
            countdown = None  # the payment is old enough that we can rely on the scheduled task alone

    if countdown is not None:
        logger.info(f"Scheduling polling of {obj.global_id} in {countdown} seconds")

        task.apply_async(args=(obj.pk, trigger_polling), countdown=countdown)
