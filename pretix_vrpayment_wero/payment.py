import hashlib
import logging
import re
import requests
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from django import forms
from django.core.cache import cache
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Order, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri

from pretix_vrpayment_wero.tasks import refresh_transaction

logger = logging.getLogger(__name__)


class VRPaymentWero(BasePaymentProvider):
    identifier = "vrpayment_wero"
    verbose_name = _("WERO via VR Payment")
    public_name = _("WERO")
    execute_payment_needs_user = True
    api_infix = ""
    test_api_url = "https://vrp-epi-payment-service-test.public.stg.vrpintegration.dev/"
    test_oauth_url = "https://keycloak-apps-test.public.apps.stg.vrpintegration.dev/"
    prod_api_url = "https://wero.api.vr-payment.digital/"
    prod_oauth_url = "https://keycloak.public.apps.prod.vr-payment.digital/"

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
            "payment_info": payment.info_data,
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
            "payment_info": payment.info_data,
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
            "payment_info": refund.info_data,
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

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        try:
            req = requests.post(
                self._get_api_url(payment.order.testmode),
                headers=self._get_auth_header(payment.order.testmode),
                json={
                    "paymentPlan": {
                        "brand": "WERO",
                        "payment": {
                            "currency": self.event.currency,
                            "amount": str(payment.amount),
                        },
                        "useCase": "EVENT_DEPENDENT",
                        "reasonForCapture": "AVAILABILITY",
                        "partialCapture": False,
                        # the documented maximum is 864000, except for the public transport sector, which has the
                        # shorter limit of 604800 seconds. VRPayment has placed at least one customer Z#23247657
                        # wrongly in this group
                        # We can live without these three extra days
                        "timeToCapture": 604800,
                        "multiCapture": False,
                    },
                    "transactionDetails": {
                        "transactionId": f"{payment.order.event.slug.upper()}-{payment.full_id}"[
                            -64:
                        ],
                        "returnUrl": build_absolute_uri(
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
                        "transactionDescription": f"{payment.full_id} {str(payment.order.event.name)}"[
                            :128
                        ],
                    },
                },
            )

            req.raise_for_status()

            # check in on payments that may never follow the redirect
            self._poll_payment(payment)

        except requests.exceptions.RequestException as e:
            try:
                payment.fail(log_data=str(e.response.text))
            except AttributeError:
                payment.fail(log_data=str(e))
            raise PaymentException(str(e))

        payment.info_data = req.json()
        payment.save(update_fields=["info"])

        return payment.info_data["landingPage"]

    def execute_refund(self, refund: OrderRefund):
        try:
            eventname = re.sub("[^a-zA-Z0-9/?:().,'+\\- ]", "", str(self.event.name))

            req = requests.post(
                self._get_api_url(
                    refund.order.testmode, "refunds", refund.payment.info_data["id"]
                ),
                headers=self._get_auth_header(refund.order.testmode),
                json={
                    "captureId": refund.payment.info_data["captures"][0]["id"],
                    "payment": {
                        "currency": refund.order.event.currency,
                        "amount": str(refund.amount),
                    },
                    "refundReason": "OTHER",
                    "transactionDescription": f"{refund.full_id} {eventname}"[:255],
                },
            )

            req.raise_for_status()
        except requests.exceptions.RequestException as e:
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save(update_fields=["state"])
            refund.order.log_action(
                "pretix.event.order.refund.failed",
                {"local_id": refund.local_id, "provider": refund.provider},
            )
            raise PaymentException(
                _("Refunding the amount via VR Payment WERO failed: {}").format(str(e))
            )

        refund.info_data = req.json()
        refund.save(update_fields=["info"])
        refund.done()

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

    def _get_api_url(self, testmode: bool, endpoint: str = None, id: str = None) -> str:
        attr = f'{"test" if testmode else "prod"}_api_url'
        base = f'{getattr(self, attr)}{self.api_infix + "/" if self.api_infix else ""}'

        return f'{base}api/v1/payments{"/" + id if id else ""}{"/" + endpoint if endpoint else ""}'

    def _get_auth_header(self, testmode: bool) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token(testmode)}",
        }

    def _get_scoped_setting(self, testmode: bool, key: str) -> str:
        return self.settings.get(f'{"test" if testmode else "prod"}_{key}')

    def _get_token(self, testmode: bool) -> str:
        client_id = self._get_scoped_setting(testmode, "client_id")
        client_secret = self._get_scoped_setting(testmode, "client_secret")

        checksum = hashlib.sha256(
            "".join([client_id, client_secret]).encode()
        ).hexdigest()
        cache_key_hash = f'pretix_vrpayment_wero_token_hash_{"test" if self.event.testmode else "prod"}_{checksum}'
        token_hash = cache.get(cache_key_hash)

        if not token_hash:
            attr = f'{"test" if self.event.testmode else "prod"}_oauth_url'
            req = requests.post(
                f"{getattr(self, attr)}auth/realms/EPI/protocol/openid-connect/token/",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )

            req.raise_for_status()
            token_hash = req.json()
            cache.set(cache_key_hash, token_hash, timeout=token_hash["expires_in"] - 10)

        return token_hash["access_token"]

    def _get_consent(self, payment: OrderPayment):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not retrieve consents. Please try again."))

        try:
            req = requests.get(
                self._get_api_url(
                    payment.order.testmode, "consents", payment.info_data["id"]
                ),
                headers=self._get_auth_header(payment.order.testmode),
            )
            req.raise_for_status()
        except requests.exceptions.RequestException as e:
            if e.response is not None and e.response.status_code == 404:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
                # poll if we don't have consent -> consent timeout is 10 minutes, after that we will fail
                self._poll_payment(payment)
            else:
                try:
                    payment.fail(log_data=str(e.response.text))
                except AttributeError:
                    payment.fail(log_data=str(e))
            raise PaymentException(str(e))
        else:
            info_data = payment.info_data
            info_data["consent"] = req.json()
            payment.info_data = info_data
            payment.save(update_fields=["info"])

            if info_data["consent"].get("status", {}).get("result") == "FAILED":
                payment.fail()
                raise PaymentException(
                    _("No consent was provided for this payment. Please try again.")
                )

        return payment

    def _get_authorizations(self, payment: OrderPayment):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(
                _("Could not retrieve authorizations. Please try again.")
            )

        try:
            req = requests.get(
                self._get_api_url(
                    payment.order.testmode, "authorizations", payment.info_data["id"]
                ),
                headers=self._get_auth_header(payment.order.testmode),
            )
            req.raise_for_status()
        except requests.exceptions.RequestException as e:
            if e.response is not None and e.response.status_code == 404:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
                # polling for authorization
                self._poll_payment(payment)
            else:
                try:
                    payment.fail(log_data=str(e.response.text))
                except AttributeError:
                    payment.fail(log_data=str(e))
            raise PaymentException(str(e))
        else:
            payment.info_data = {**payment.info_data, **req.json()}
            payment.save(update_fields=["info"])

        return payment

    def _capture(self, payment: OrderPayment):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not capture payment. Please try again."))

        try:
            req = requests.post(
                self._get_api_url(
                    payment.order.testmode, "captures", payment.info_data["id"]
                ),
                headers=self._get_auth_header(payment.order.testmode),
                json={
                    "merchantTransactionId": f"{payment.order.event.slug.upper()}-{payment.full_id}"[
                        -64:
                    ],
                    "payment": {
                        "currency": self.event.currency,
                        "amount": str(payment.amount),
                    },
                    "finalCapture": True,
                },
            )
            req.raise_for_status()
            # processing captures takes a few seconds
            self._poll_payment(payment)
        except requests.exceptions.RequestException as e:
            if e.response is not None and e.response.status_code not in (400, 403):
                # This can likely be healed by polling, otherwise periodic task will expire later
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
            else:
                try:
                    payment.fail(log_data=str(e.response.text))
                except AttributeError:
                    payment.fail(log_data=str(e))
                raise PaymentException(str(e))
        else:
            payment.info_data["captures"] = [req.json()]
            payment.save(update_fields=["info"])

            return self._get_captures(payment, is_retry=True)

    def _get_captures(self, payment: OrderPayment, is_retry=False):
        if "id" not in payment.info_data:
            payment.fail()
            raise PaymentException(_("Could not validate payment. Please try again."))

        try:
            req = requests.get(
                self._get_api_url(
                    payment.order.testmode, "captures", payment.info_data["id"]
                ),
                headers=self._get_auth_header(payment.order.testmode),
            )
            req.raise_for_status()
        except requests.exceptions.RequestException as e:
            if e.response is not None and e.response.status_code == 404:
                payment.state = OrderPayment.PAYMENT_STATE_PENDING
                payment.save(update_fields=["state"])
                # we don't call refresh_transaction.apply_async here because we first call
                # _get_captures and only if it returns None, then call _capture() which takes
                # care of polling if there wasn't a capture at all
                return None
            else:
                raise PaymentException(str(e))
        else:
            payment.info_data = {**payment.info_data, **req.json()}
            payment.save(update_fields=["info"])
            self._process_captures(payment)
            # we poll here while the captures is not approved and if the _capture isn't the source of this _get_captures
            # call
            payment.refresh_from_db()
            if payment.state == OrderPayment.PAYMENT_STATE_PENDING and not is_retry:
                self._poll_payment(payment)

        return payment

    def _process_captures(self, payment: OrderPayment):
        if payment.info_data["captures"][-1]["status"]["result"] == "FAILED":
            payment.fail()
            raise PaymentException(
                _(
                    "The payment failed without an error message. You can click below to try again."
                )
            )
        elif payment.info_data["captures"][-1]["status"]["result"] == "PENDING":
            payment.state = OrderPayment.PAYMENT_STATE_PENDING
            payment.save(update_fields=["state"])
        elif payment.info_data["captures"][-1]["status"]["result"] == "SUCCESSFUL":
            payment.confirm()

    def _poll_payment(self, payment: OrderPayment):
        payment_age = now() - payment.created

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
            logger.info(
                f"Scheduling polling of {payment.global_id} in {countdown} seconds"
            )
            refresh_transaction.apply_async(args=(payment.pk,), countdown=countdown)
