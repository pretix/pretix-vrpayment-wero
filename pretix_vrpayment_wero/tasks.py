import logging
from django.db import transaction
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.celery_app import app
from pretix.helpers import OF_SELF

logger = logging.getLogger(__name__)


@app.task()
@scopes_disabled()
@transaction.atomic
def refresh_transaction(payment):
    payment = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment)
    pprov = payment.payment_provider

    try:
        payment.info_data["id"]
    except KeyError as e:
        payment.fail(log_data=str(e))
        return

    capture_payment = None
    try:
        capture_payment = pprov._get_captures(payment)
    except Exception:  # NoQA
        # The gateway might be down, something else might be not working.
        # We do not fail the payment so that we can try again later.
        logger.exception(f"Failed get captures for payment {payment.full_id}")
        return

    if not capture_payment:
        try:
            pprov._capture(payment)
        except Exception:  # NoQA
            logger.exception(f"Failed to actively capture payment {payment.full_id}")
            pass
