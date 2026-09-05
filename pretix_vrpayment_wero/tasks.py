import logging
from django.db import transaction
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.base.payment import PaymentException
from pretix.celery_app import app
from pretix.helpers import OF_SELF

logger = logging.getLogger(__name__)


@app.task()
@scopes_disabled()
@transaction.atomic
def refresh_transaction(payment):
    payment = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment)
    pprov = payment.payment_provider

    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        return

    try:
        payment.info_data["id"]
    except KeyError as e:
        payment.fail(log_data=str(e))
        return

    payment.info_data = {
        **payment.info_data,
        **{"last_polling": now().isoformat()},
    }
    payment.save(update_fields=["info"])

    try:
        pprov._get_consent(payment)
    except PaymentException:
        return

    payment.refresh_from_db()

    try:
        pprov._get_authorizations(payment)
    except PaymentException:
        return

    payment.refresh_from_db()

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
