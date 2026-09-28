import hashlib
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.functional import cached_property
from django.views import View
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from pretix.multidomain.urlreverse import eventreverse

from pretix_vrpayment_wero.client import WeroApiError
from pretix_vrpayment_wero.tasks import refresh_transaction


class VRPaymentWeroOrderView:
    def dispatch(self, request, *args, **kwargs):
        try:
            self.order = request.event.orders.get(code=kwargs["order"])
            if (
                hashlib.sha1(self.order.secret.lower().encode()).hexdigest()
                != kwargs["hash"].lower()
            ):
                raise Http404("")
        except Order.DoesNotExist:
            # Do a hash comparison as well to harden timing attacks
            if (
                "abcdefghijklmnopq".lower()
                == hashlib.sha1("abcdefghijklmnopq".encode()).hexdigest()
            ):
                raise Http404("")
            else:
                raise Http404("")
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def payment(self):
        return get_object_or_404(
            self.order.payments,
            pk=self.kwargs["payment"],
            provider=self.kwargs["payment_provider"],
        )

    @cached_property
    def pprov(self):
        return self.request.event.get_payment_providers()[self.payment.provider]

    def _redirect_to_order(self):
        self.order.refresh_from_db()
        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )


class ReturnView(VRPaymentWeroOrderView, View):
    def get(self, request, *args, **kwargs):

        if self.payment.state in (
            OrderPayment.PAYMENT_STATE_CONFIRMED,
            OrderPayment.PAYMENT_STATE_CANCELED,
            OrderPayment.PAYMENT_STATE_FAILED,
        ):
            return self._redirect_to_order()

        try:
            click = int(request.GET.get("click", 0))
        except ValueError:
            click = 0

        try:
            successful_capture = refresh_transaction(self.payment.pk, False)
            if not successful_capture and click < 5:
                return render(
                    request,
                    "pretix_vrpayment_wero/return.html",
                    {
                        "order": self.payment.order,
                    },
                )

        except WeroApiError:
            return self._redirect_to_order()
        except PaymentException:
            return self._redirect_to_order()

        return self._redirect_to_order()
