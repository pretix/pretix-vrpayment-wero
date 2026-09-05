import logging
from datetime import timedelta
from django.dispatch import receiver
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Event, OrderPayment
from pretix.base.signals import periodic_task, register_payment_providers

from .tasks import refresh_transaction

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="vrpayment_wero")
def register_payment_provider(sender, **kwargs):
    from .payment import VRPaymentWero

    return VRPaymentWero


@receiver(periodic_task, dispatch_uid="vrpayment_wero_periodic_task")
@scopes_disabled()
def periodic_task(sender, **kwargs):
    return periodic_task_handler(
        __name__.split(".")[0], "vrpayment_wero", sender, **kwargs
    )


def periodic_task_handler(package, identifier, sender, **kwargs):
    for ev in Event.objects.filter(plugins__contains=package):
        if not ev.settings.get(f"payment_{identifier}__enabled"):
            continue

        # We check for up to a day, as we have observed settlements taking multiple hours
        # Also make sure to fail any payments that are older than 1 hour to enable retry in order-view.
        payments_qs = OrderPayment.objects.filter(
            order__event=ev,
            provider=identifier,
        )

        for payment in payments_qs.filter(
            state__in=[
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
            ],
            created__lt=now() - timedelta(hours=1),
        ):
            payment.fail(log_data="Payment expired.")

        for payment in payments_qs.filter(
            state__in=[
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_CANCELED,
                OrderPayment.PAYMENT_STATE_FAILED,
            ],
            created__gte=now() - timedelta(hours=24),
        ):
            refresh_transaction.apply_async(args=(payment.pk,))
