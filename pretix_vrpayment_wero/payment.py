from typing import Any, List, Union

import hashlib
import logging
import re
from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Order, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri

from pretix_vrpayment_wero.client import (
    Authorization,
    CreatedRefund,
    GeneralError,
    Refund,
    Result,
    WeroApiError,
    WeroClient,
)
from pretix_vrpayment_wero.info_data import (
    load_payment_info_data,
    load_refund_info_data,
    payment_info,
    refund_info,
)
from pretix_vrpayment_wero.tasks import refresh_refund, refresh_transaction

logger = logging.getLogger(__name__)


class VRPaymentWero(BasePaymentProvider):
    identifier = "vrpayment_wero"
    verbose_name = _("WERO via VR Payment")
    public_name = _("WERO")
    execute_payment_needs_user = True

    @property
    def settings_form_fields(self) -> dict:
        fields = [
            (
                "test_client_id",
                forms.CharField(
                    label=_("Test Client ID"),
                    required=False,
                ),
            ),
            (
                "test_client_secret",
                SecretKeySettingsField(
                    label=_("Test Client Secret"),
                    required=False,
                ),
            ),
            (
                "prod_client_id",
                forms.CharField(
                    label=_("Production Client ID"),
                    required=False,
                ),
            ),
            (
                "prod_client_secret",
                SecretKeySettingsField(
                    label=_("Production Client Secret"),
                    required=False,
                ),
            ),
        ]
        d = OrderedDict(fields + list(super().settings_form_fields.items()))
        d.move_to_end("_enabled", last=False)

        return d

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return "id" in payment.info_data

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return "id" in payment.info_data

    def payment_pending_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        template = get_template("pretix_vrpayment_wero/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "order": payment.order,
            "payment": payment,
            "payment_info": load_payment_info_data(payment).to_dict(),
        }
        return template.render(ctx)

    def payment_control_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        template = get_template("pretix_vrpayment_wero/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": load_payment_info_data(payment).to_dict(),
            "payment": payment,
            "provider": self,
        }
        return template.render(ctx)

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        return payment.info_data["id"]

    def api_payment_details(self, payment: OrderPayment):
        return {"id": payment.info_data.get("id")}

    def refund_control_render(self, request: HttpRequest, refund: OrderRefund) -> str:
        template = get_template("pretix_vrpayment_wero/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": load_refund_info_data(refund).to_dict(),
            "payment": refund,
            "provider": self,
        }
        return template.render(ctx)

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        return refund.info_data["id"]

    def api_refund_details(self, refund: OrderRefund):
        return {"id": refund.info_data.get("id")}

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return True

    def checkout_confirm_render(
        self, request, order: Order = None, info_data: dict = None
    ) -> str:
        template = get_template("pretix_vrpayment_wero/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "info_data": info_data,
            "provider": self,
        }
        return template.render(ctx)

    def payment_form_render(
        self, request: HttpRequest, total: Decimal, order: Order = None
    ) -> str:
        template = get_template("pretix_vrpayment_wero/checkout_payment_form.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
        }
        return template.render(ctx)

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return (
            super().is_allowed(request, total)
            and self._get_scoped_setting(self.event.testmode, "client_id")
            and self._get_scoped_setting(self.event.testmode, "client_secret")
        )

    def order_change_allowed(self, order: Order, request: HttpRequest = None) -> bool:
        return (
            super().order_change_allowed(order, request)
            and self._get_scoped_setting(order.testmode, "client_id")
            and self._get_scoped_setting(order.testmode, "client_secret")
        )

    @property
    def test_mode_message(self):
        if self._get_scoped_setting(True, "client_id") and self._get_scoped_setting(
            True, "client_secret"
        ):
            return mark_safe(
                _(
                    "The VR Payment WERO plugin is operating in test mode. You can only use WERO test wallets to perform"
                    "transactions. On the WERO payment page, obtain a <em>WERO code</em> and use it on the "
                    "<a {args}>Example Consumer PSP interface</a>, to generate and approve the payment consent. "
                    "No money will actually be transferred."
                ).format(
                    args='href="https://example-consumer-psps.werouat.eu/wallets/137e8447-f0f1-40c7-a56e-a33c25759cb3/payment-means/6cbc89a2-b84e-42ea-bd2e-c1197bf2b8a3/consents/create" '  # noQA
                    'target="_blank"'
                )
            )
        return None

    def get_wero_client(self, obj: Union[OrderPayment, OrderRefund]) -> WeroClient:
        return WeroClient(
            self._get_scoped_setting(True, "client_id"),
            self._get_scoped_setting(True, "client_secret"),
            self._get_scoped_setting(False, "client_id"),
            self._get_scoped_setting(False, "client_secret"),
            obj.order.testmode,
        )

    def _get_scoped_setting(self, testmode: bool, key: str) -> str:
        return self.settings.get(f'{"test" if testmode else "prod"}_{key}')

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        try:
            client = self.get_wero_client(payment)
            resp = client.create_payment(
                f"{payment.order.event.slug.upper()}-{payment.full_id}",
                self.event.currency,
                payment.amount,
                build_absolute_uri(
                    self.event,
                    f"plugins:pretix_{self.identifier}:return",
                    kwargs={
                        "order": payment.order.code,
                        "payment": payment.pk,
                        "hash": hashlib.sha1(
                            payment.order.secret.lower().encode()
                        ).hexdigest(),
                        "payment_provider": self.identifier,
                    },
                ),
                f"{payment.full_id} {str(payment.order.event.name)}",
            )

            with payment_info(payment, save=True) as info:
                info.id = resp.id
                info.payment = resp

            return resp.landing_page
        except (WeroApiError, GeneralError) as e:
            if isinstance(e, WeroApiError):
                message = e.wero_error.message
            else:
                try:
                    message = e.response.text
                except AttributeError:
                    message = str(e.response)

            payment.fail(log_data=message)
            raise PaymentException(message)
        finally:
            payment.save(update_fields=["info", "state"])
            # check in on payments that may never follow the redirect
            refresh_transaction.apply_async(args=(payment.pk, True), countdown=30)

    def get_consent(self, payment: OrderPayment):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not retrieve consents. Please try again."))

        try:
            client = self.get_wero_client(payment)
            consent = client.get_consent(payment.info_data["id"])

            with payment_info(payment) as info_data:
                info_data.consent = consent

            if consent.status.result == Result.PENDING:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                return None
            if consent.status.result == Result.FAILED:
                payment.fail(log_data=str(consent))
                raise PaymentException(
                    _("No consent was provided for this payment. Please try again.")
                )
            if consent.status.result == Result.SUCCESSFUL:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                return consent

        except WeroApiError as e:
            if not e.retry:
                payment.fail(log_data=e.wero_error.message)
            raise e

        finally:
            payment.save(update_fields=["state", "info"])

        raise NotImplementedError(consent)

    def get_authorizations(self, payment: OrderPayment) -> List[Authorization] | None:
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(
                _("Could not retrieve authorizations. Please try again.")
            )

        try:
            client = self.get_wero_client(payment)
            authorizations = client.get_authorizations(payment.info_data["id"])

            with payment_info(payment) as info_data:
                info_data.authorizations = authorizations

            pending = [auth.status.result == Result.PENDING for auth in authorizations]
            if any(pending):
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                return None

            failed = [auth.status.result == Result.FAILED for auth in authorizations]
            if any(failed):
                payment.fail(log_data=str(authorizations))
                raise PaymentException(
                    _("Authorization failed for this payment. Please try again.")
                )

            success = [
                auth.status.result == Result.SUCCESSFUL for auth in authorizations
            ]
            if any(success):
                return authorizations

        except WeroApiError as e:
            if not e.retry:
                payment.fail(log_data=e.wero_error.message)
            raise e
        finally:
            payment.save(update_fields=["state", "info"])

        raise NotImplementedError(authorizations)

    def capture(self, payment: OrderPayment):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not capture payment. Please try again."))

        try:
            client = self.get_wero_client(payment)
            capture = client.create_capture(
                payment.info_data["id"],
                f"{payment.order.event.slug.upper()} - {payment.full_id}",
                self.event.currency,
                payment.amount,
            )
            with payment_info(payment) as info_data:
                info_data.capture = capture

        except WeroApiError as e:
            if not e.retry:
                payment.fail(log_data=e.wero_error.message)
            raise e
        finally:
            payment.save(update_fields=["state", "info"])

        return capture

    def get_captures(self, payment: OrderPayment, authorizations: List[Authorization]):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not validate payment. Please try again."))

        try:
            client = self.get_wero_client(payment)
            captures = client.get_captures(payment.info_data["id"])

            with payment_info(payment) as info_data:
                info_data.captures = captures

            successful_authorizations = [
                auth.id
                for auth in authorizations
                if auth.status.result == Result.SUCCESSFUL
            ]
            last_capture = captures[-1]

            if last_capture.status.result == Result.FAILED:
                payment.fail()
                raise PaymentException(
                    _(
                        "The payment failed without an error message. You can click below to try again."
                    )
                )
            elif last_capture.status.result == Result.PENDING:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                return None
            elif (
                last_capture.status.result == Result.SUCCESSFUL
                and last_capture.authorization_id in successful_authorizations
            ):
                payment.confirm()

            return captures
        except WeroApiError as e:
            if not e.retry:
                payment.fail(log_data=e.wero_error.message)
            raise e
        finally:
            payment.save(update_fields=["state", "info"])

    def _fail_refund(self, refund: OrderRefund, log_data: Any):
        refund.state = OrderRefund.REFUND_STATE_FAILED

        refund.order.log_action(
            "pretix.event.order.refund.failed",
            {
                "local_id": refund.local_id,
                "provider": self.identifier,
                "info": refund.info,
                "data": log_data,
            },
        )

        refund.save(update_fields=["state"])

    def _process_refund(self, refund: OrderRefund, wero_refund: Refund | CreatedRefund):
        if wero_refund.status.result == Result.FAILED:
            self._fail_refund(refund, log_data=wero_refund.to_dict())
        elif wero_refund.status.result == Result.PENDING:
            pass
        elif wero_refund.status.result == Result.SUCCESSFUL:
            if wero_refund.status.detail == "REFUND_SETTLED":
                refund.done()
        else:
            raise NotImplementedError(wero_refund)

    def get_refunds(self, refund: OrderRefund):
        if "id" not in refund.info_data:
            self._fail_refund(refund, "")
            raise PaymentException(_("Could not retrieve refund_id. Please try again."))

        try:
            client = self.get_wero_client(refund)

            with refund_info(refund=refund) as info_data:
                resp = client.get_refunds(
                    refund.payment.info_data["id"],
                )

                relevant_refunds = []
                for ref in resp:
                    if ref.id == info_data.id:
                        relevant_refunds.append(ref)

                if len(relevant_refunds) > 1:
                    self._fail_refund(
                        refund, log_data="Multiple matching refunds in API response."
                    )
                    raise PaymentException(
                        _("Multiple matching refunds in API response.")
                    )

                info_data.refunds = relevant_refunds

            self._process_refund(refund, relevant_refunds[0])

        except GeneralError as e:
            raise e
        except WeroApiError as e:
            if not e.retry:
                self._fail_refund(refund, log_data=e.wero_error.message)
            raise e
        finally:
            refund.save(update_fields=["info", "state"])

    def execute_refund(self, refund: OrderRefund):
        try:
            eventname = re.sub("[^a-zA-Z0-9/?:().,'+\\- ]", "", str(self.event.name))

            client = self.get_wero_client(refund)
            with refund_info(refund=refund) as refund_info_data:
                with payment_info(payment=refund.payment) as payment_info_data:
                    capture_id = payment_info_data.captures[0].id
                    resp = client.create_refund(
                        payment_info_data.id,
                        capture_id,
                        refund.order.event.currency,
                        refund.amount,
                        f"{refund.full_id} {eventname}",
                    )
                    refund_info_data.id = resp.id
                    refund_info_data.refund = resp
                    refund_info_data.capture_id = capture_id
                    refund.state = OrderRefund.REFUND_STATE_TRANSIT
            self._process_refund(refund, resp)
        except (WeroApiError, GeneralError) as e:
            if isinstance(e, WeroApiError):
                message = e.wero_error.message
            else:
                try:
                    message = e.response.text
                except AttributeError:
                    message = str(e.response)

            self._fail_refund(refund, log_data=message)
            raise PaymentException(message)
        finally:
            refund.save(update_fields=["info", "state"])
            refund.refresh_from_db()
            if refund.state != OrderRefund.REFUND_STATE_DONE:
                refresh_refund(refund.id, True)
