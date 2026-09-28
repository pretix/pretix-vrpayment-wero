from typing import Any, Dict, List, Literal, Optional

import hashlib
import json
import logging
import requests
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from django.core.cache import cache
from django.core.serializers.base import DeserializationError
from enum import Enum
from requests import Response
from rest_framework import serializers
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeroError:
    type: str = ""
    title: str = ""
    status: int = 0
    detail: str = ""
    instance: str = ""

    def to_dict(self) -> dict:
        return WeroErrorSerializer(self).data

    @property
    def message(self) -> str:
        return (
            f"{self.status}: {self.title} - {self.detail}; {self.type}; {self.instance}"
        )


class WeroApiError(requests.exceptions.RequestException):
    """ """

    retry: bool = False
    wero_error: WeroError

    def __init__(
        self,
        *args: Any,
        wero_error: WeroError,
        retry: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self.wero_error = wero_error
        if retry is not None:
            self.retry = retry
        super().__init__(*args, **kwargs)


class BadRequestError(WeroApiError):
    retry: bool = False


class ProcessingError(WeroApiError):
    retry: bool = False


class NotFoundError(WeroApiError):
    retry: bool = True


class InternalServerError(WeroApiError):
    retry: bool = True


class GeneralError(requests.exceptions.RequestException):
    retry: bool = True


class Result(str, Enum):
    PENDING = "PENDING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"


class RefundType(str, Enum):
    FULL = "FULL_REFUND"
    PARTIAL = "PARTIAL_REFUND"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def to_dict(self) -> dict:
        return MoneySerializer(self).data

    def __post_init__(self):
        if self.currency != "EUR":
            raise DeserializationError("$.payment.currency", "only EUR is supported")
        if not (Decimal("0.01") <= self.amount <= Decimal(100000)):
            raise DeserializationError("$.payment.amount", "out of range")
        if self.amount != self.amount.quantize(Decimal("0.01")):
            raise DeserializationError("$.payment.amount", "more than 2 decimals")


@dataclass(frozen=True)
class Status:
    result: Result
    detail: str
    description: str
    code: str | None = None  # deprecated in the spec

    def to_dict(self) -> dict:
        return StatusSerializer(self).data


@dataclass(frozen=True)
class CreatedPayment:
    id: UUID
    landing_page: str

    def to_dict(self) -> dict:
        return CreatedPaymentSerializer(self).data


@dataclass(frozen=True)
class Consent:
    id: UUID
    payment: Money
    created_at: datetime
    status: Status

    def to_dict(self) -> dict:
        return ConsentSerializer(self).data


@dataclass(frozen=True)
class Authorization:
    id: UUID
    payment: Money
    created_at: datetime
    status: Status

    def to_dict(self) -> dict:
        return AuthorizationSerializer(self).data


@dataclass(frozen=True)
class CreatedCapture:
    id: UUID
    created_at: datetime
    status: Status

    def to_dict(self) -> dict:
        return CreatedCaptureSerializer(self).data


@dataclass(frozen=True)
class Capture:
    id: UUID
    authorization_id: UUID
    merchant_transaction_id: str
    settled_at: Optional[datetime]
    payment: Money
    created_at: datetime
    status: Status

    def to_dict(self) -> dict:
        return CaptureSerializer(self).data


@dataclass(frozen=True)
class CreatedRefund:
    id: UUID
    created_at: datetime
    status: Status

    def to_dict(self) -> dict:
        return CreatedRefundSerializer(self).data


@dataclass(frozen=True)
class Refund:
    id: UUID
    created_at: datetime
    payment: Money
    capture_id: UUID
    type: RefundType
    status: Status

    def to_dict(self) -> dict:
        return RefundSerializer(self).data


def _camelize(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(word.capitalize() for word in tail)


class CamelCaseSerializer(serializers.Serializer):
    """Declares fields in snake_case, reads/writes them as camelCase."""

    def get_fields(self):
        fields = {}
        for name, field in super().get_fields().items():
            key = _camelize(name)
            if key != name and field.source is None:
                field.source = name
            fields[key] = field
        return fields


class MoneySerializer(CamelCaseSerializer):
    amount = serializers.DecimalField(
        max_digits=8, decimal_places=2, coerce_to_string=True
    )
    currency = serializers.ChoiceField(choices=["EUR"])


class StatusSerializer(CamelCaseSerializer):
    result = serializers.ChoiceField(choices=Result)
    detail = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    code = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class WeroErrorSerializer(CamelCaseSerializer):
    type = serializers.CharField(allow_blank=True)
    title = serializers.CharField(allow_blank=True)
    status = serializers.IntegerField(allow_null=True)
    detail = serializers.CharField(allow_blank=True)
    instance = serializers.CharField(allow_blank=True)

    @staticmethod
    def to_domain(d) -> WeroError:
        return WeroError(
            type=d.get("type", ""),
            title=d.get("title", ""),
            status=d.get("status", 0),
            detail=d.get("detail", ""),
            instance=d.get("instance", ""),
        )

    def build(self) -> WeroError:
        return self.to_domain(self.validated_data)


class CreatedPaymentSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    landing_page = serializers.CharField()

    @staticmethod
    def to_domain(d) -> CreatedPayment:
        return CreatedPayment(id=d["id"], landing_page=d["landing_page"])

    def build(self) -> CreatedPayment:
        return self.to_domain(self.validated_data)


class ConsentSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    payment = MoneySerializer()
    created_at = serializers.DateTimeField()
    status = StatusSerializer()

    @staticmethod
    def to_domain(d) -> Consent:
        return Consent(
            id=d["id"],
            payment=Money(**d["payment"]),
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> Consent:
        return self.to_domain(self.validated_data)


class AuthorizationSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    payment = MoneySerializer()
    created_at = serializers.DateTimeField()
    status = StatusSerializer()

    @staticmethod
    def to_domain(d) -> Authorization:
        return Authorization(
            id=d["id"],
            payment=Money(**d["payment"]),
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> Authorization:
        return self.to_domain(self.validated_data)


class CreatedCaptureSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    status = StatusSerializer()
    created_at = serializers.DateTimeField()

    @staticmethod
    def to_domain(d) -> CreatedCapture:
        return CreatedCapture(
            id=d["id"],
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> CreatedCapture:
        return self.to_domain(self.validated_data)


class CaptureSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    authorization_id = serializers.UUIDField()
    merchant_transaction_id = serializers.CharField(allow_blank=True)
    settled_at = serializers.DateTimeField(required=False, allow_null=True)
    payment = MoneySerializer()
    status = StatusSerializer()
    created_at = serializers.DateTimeField()

    @staticmethod
    def to_domain(d) -> Capture:
        return Capture(
            id=d["id"],
            merchant_transaction_id=d["merchant_transaction_id"],
            authorization_id=d["authorization_id"],
            settled_at=d.get("settled_at"),
            payment=Money(**d["payment"]),
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> Capture:
        return self.to_domain(self.validated_data)


class CreatedRefundSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    status = StatusSerializer()

    @staticmethod
    def to_domain(d) -> CreatedRefund:
        return CreatedRefund(
            id=d["id"],
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> CreatedRefund:
        return self.to_domain(self.validated_data)


class RefundSerializer(CamelCaseSerializer):
    id = serializers.UUIDField()
    payment = MoneySerializer()
    capture_id = serializers.UUIDField()
    type = serializers.ChoiceField([(c.value, c.name) for c in RefundType])
    created_at = serializers.DateTimeField()
    status = StatusSerializer()

    @staticmethod
    def to_domain(d) -> Refund:
        return Refund(
            id=d["id"],
            payment=Money(**d["payment"]),
            capture_id=d["capture_id"],
            type=d["type"],
            created_at=d["created_at"],
            status=Status(**d["status"]),
        )

    def build(self) -> Refund:
        return self.to_domain(self.validated_data)


class WeroClient:
    test_api_url = "https://vrp-epi-payment-service-test.public.stg.vrpintegration.dev/"
    test_oauth_url = "https://keycloak-apps-test.public.apps.stg.vrpintegration.dev/"
    prod_api_url = "https://wero.api.vr-payment.digital/"
    prod_oauth_url = "https://keycloak.public.apps.prod.vr-payment.digital/"
    api_infix = ""
    merchant_id = None

    def __init__(
        self,
        test_client_id: str,
        test_client_secret: str,
        client_id: str,
        client_secret: str,
        testmode: bool,
        tsp: bool = False,
        merchant_id: Optional[str] = None,
    ):
        self.testmode = testmode
        if testmode:
            self.client_id = test_client_id
            self.client_secret = test_client_secret
        else:
            self.client_id = client_id
            self.client_secret = client_secret
        if tsp:
            self.api_infix = "tsp"
            self.merchant_id = merchant_id

    def _get_token(self):
        client_id = self.client_id
        client_secret = self.client_secret

        checksum = hashlib.sha256(
            "".join([client_id, client_secret]).encode()
        ).hexdigest()
        cache_key_hash = f'pretix_vrpayment_wero_token_hash_{"test" if self.testmode else "prod"}_{checksum}'
        token_hash = cache.get(cache_key_hash)

        if not token_hash:
            attr = f'{"test" if self.testmode else "prod"}_oauth_url'
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

    @property
    def auth_header(self):
        auth = {
            "Authorization": f"Bearer {self._get_token()}",
        }

        if self.merchant_id:
            merchant = {"merchantId": self.merchant_id}
            return {**merchant, **auth}
        else:
            return auth

    def _get_api_url(
        self,
        endpoint: (
            None | Literal["refunds", "consents", "authorizations", "captures"]
        ) = None,
        id: Optional[uuid.UUID] = None,
    ) -> str:
        attr = f'{"test" if self.testmode else "prod"}_api_url'
        base = f'{getattr(self, attr)}{self.api_infix + "/" if self.api_infix else ""}'

        return f'{base}api/v1/payments{"/" + str(id) if id else ""}{"/" + endpoint if endpoint else ""}'

    def post(
        self,
        endpoint: Literal["captures", "refunds"] | None,
        wero_id: Optional[uuid.UUID],
        body: Dict[Any, Any],
    ):
        try:
            resp = requests.post(
                self._get_api_url(endpoint, wero_id),
                headers=self.auth_header,
                json=body,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:

            if e.response is not None:
                error: WeroError
                if e.response.status_code in [400, 404, 422, 500]:
                    payload = json.loads(e.response.text, parse_float=Decimal)
                    s = WeroErrorSerializer(data=payload)
                    s.is_valid(raise_exception=True)
                    error = s.build()
                    logger.exception(error.message)
                    if e.response.status_code == 400:
                        raise BadRequestError(e, wero_error=error)
                    elif e.response.status_code == 404:
                        raise NotFoundError(e, wero_error=error)
                    elif e.response.status_code == 422:
                        raise ProcessingError(e, wero_error=error)
                    elif e.response.status_code == 500:
                        raise InternalServerError(e, wero_error=error)

            logger.exception(e)
            raise GeneralError(e)

    def get(
        self,
        endpoint: Literal["refunds", "consents", "authorizations", "captures"],
        wero_id: uuid.UUID,
    ) -> Response:
        try:
            resp = requests.get(
                self._get_api_url(endpoint, wero_id),
                headers=self.auth_header,
            )
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if e.response is not None:
                error: WeroError
                if e.response.status_code in [400, 404, 422, 500]:
                    payload = json.loads(e.response.text, parse_float=Decimal)
                    s = WeroErrorSerializer(data=payload)
                    s.is_valid(raise_exception=True)
                    error = s.build()
                    logger.exception(error.message)
                    if e.response.status_code == 400:
                        raise BadRequestError(e, wero_error=error)
                    elif e.response.status_code == 404:
                        raise NotFoundError(e, wero_error=error)
                    elif e.response.status_code == 422:
                        raise ProcessingError(e, wero_error=error)
                    elif e.response.status_code == 500:
                        raise InternalServerError(e, wero_error=error)

            logger.exception(e)
            raise GeneralError(e)

    def _get_list(self, endpoint, wero_id: uuid.UUID, serializer_class):
        resp = self.get(endpoint, wero_id)
        payload = json.loads(resp.text, parse_float=Decimal)
        if isinstance(payload, dict):
            payload = payload[endpoint]
        s = serializer_class(data=payload, many=True)
        s.is_valid(raise_exception=True)
        return [serializer_class.to_domain(d) for d in s.validated_data]

    def get_authorizations(self, wero_id: uuid.UUID) -> List[Authorization]:
        return self._get_list("authorizations", wero_id, AuthorizationSerializer)

    def get_captures(self, wero_id: uuid.UUID) -> List[Capture]:
        return self._get_list("captures", wero_id, CaptureSerializer)

    def get_refunds(self, wero_id: uuid.UUID) -> List[Refund]:
        return self._get_list("refunds", wero_id, RefundSerializer)

    def get_consent(self, wero_id: uuid.UUID) -> Consent:
        resp = self.get("consents", wero_id)
        payload = json.loads(resp.text, parse_float=Decimal)
        s = ConsentSerializer(data=payload)
        s.is_valid(raise_exception=True)
        return s.build()

    def create_capture(
        self, wero_id: uuid.UUID, order_payment_id: str, currency: str, amount: Decimal
    ) -> CreatedCapture:
        body = {
            "merchantTransactionId": order_payment_id[-64:],
            "payment": {
                "currency": currency,
                "amount": str(amount),
            },
            "finalCapture": True,
        }
        resp = self.post("captures", wero_id, body)
        payload = json.loads(resp.text, parse_float=Decimal)
        s = CreatedCaptureSerializer(data=payload)
        s.is_valid(raise_exception=True)
        return s.build()

    def create_payment(
        self,
        order_payment_id: str,
        currency: str,
        amount: Decimal,
        return_url: str,
        description: str,
    ) -> CreatedPayment:
        body = {
            "paymentPlan": {
                "brand": "WERO",
                "payment": {
                    "currency": currency,
                    "amount": str(amount),
                },
                "useCase": "EVENT_DEPENDENT",
                "reasonForCapture": "AVAILABILITY",
                "partialCapture": False,
                "timeToCapture": 3600,  # should be equal to the longest time the cart can be active
                "multiCapture": False,
            },
            "transactionDetails": {
                "transactionId": order_payment_id[-64:],
                "returnUrl": return_url,
                "transactionDescription": description[:128],
            },
        }
        resp = self.post(None, None, body)
        payload = json.loads(resp.text, parse_float=Decimal)
        s = CreatedPaymentSerializer(data=payload)
        s.is_valid(raise_exception=True)
        return s.build()

    def create_refund(
        self,
        wero_id: uuid.UUID,
        capture_id: uuid.UUID,
        currency: str,
        amount: Decimal,
        description: str,
    ) -> CreatedRefund:
        body = {
            "captureId": str(capture_id),
            "payment": {
                "currency": currency,
                "amount": str(amount),
            },
            "refundReason": "OTHER",
            "transactionDescription": description[:255],
        }
        resp = self.post("refunds", wero_id, body)
        payload = json.loads(resp.text, parse_float=Decimal)
        s = CreatedRefundSerializer(data=payload)
        s.is_valid(raise_exception=True)
        return s.build()
