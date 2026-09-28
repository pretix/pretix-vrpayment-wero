from typing import Dict, List, Optional

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OrderPayment, OrderRefund
from rest_framework import serializers
from uuid import UUID

from pretix_vrpayment_wero.client import (
    Authorization,
    AuthorizationSerializer,
    Capture,
    CaptureSerializer,
    Consent,
    ConsentSerializer,
    CreatedCapture,
    CreatedCaptureSerializer,
    CreatedPayment,
    CreatedPaymentSerializer,
    CreatedRefund,
    CreatedRefundSerializer,
    Refund,
    RefundSerializer,
    Result,
    Status,
)

logger = logging.getLogger(__name__)


@dataclass
class WeroPaymentInfo:
    id: Optional[UUID]
    last_polling: Optional[datetime]
    payment: Optional[CreatedPayment]
    consent: Optional[Consent]
    authorizations: Optional[List[Authorization]]
    capture: Optional[CreatedCapture]
    captures: Optional[List[Capture]]

    @property
    def status(self) -> Status | None:
        if self.captures:
            return self.captures[-1].status
        if self.authorizations:
            return self.authorizations[-1].status
        if self.consent:
            return self.consent.status
        return None

    def failure_message(self) -> str | None:
        current_status = self.status
        if current_status is not None and current_status.result == Result.FAILED:
            if current_status.description != "":
                return current_status.description
            else:
                return _("The payment failed without an error message.")
        return None

    def to_dict(self) -> dict:
        return WeroPaymentInfoSerializer(self).data


class WeroPaymentInfoSerializer(serializers.Serializer):
    id = serializers.UUIDField(allow_null=True)
    last_polling = serializers.DateTimeField(required=False, allow_null=True)
    payment = CreatedPaymentSerializer(required=False, allow_null=True)
    consent = ConsentSerializer(required=False, allow_null=True)
    authorizations = AuthorizationSerializer(many=True, required=False, allow_null=True)
    capture = CreatedCaptureSerializer(required=False, allow_null=True)
    captures = CaptureSerializer(many=True, required=False, allow_null=True)

    @staticmethod
    def to_domain(d) -> "WeroPaymentInfo":
        payment = d.get("payment")
        authorizations = d.get("authorizations")
        capture = d.get("capture")
        captures = d.get("captures")
        consent = d.get("consent")
        return WeroPaymentInfo(
            id=d["id"],
            last_polling=d.get("last_polling"),
            payment=CreatedPaymentSerializer.to_domain(payment) if payment else None,
            consent=ConsentSerializer.to_domain(consent) if consent else None,
            authorizations=(
                [AuthorizationSerializer.to_domain(a) for a in authorizations]
                if authorizations is not None
                else None
            ),
            capture=CreatedCaptureSerializer.to_domain(capture) if capture else None,
            captures=(
                [CaptureSerializer.to_domain(c) for c in captures]
                if captures is not None
                else None
            ),
        )

    def build(self) -> "WeroPaymentInfo":
        return self.to_domain(self.validated_data)


def load_payment_info_data(payment: OrderPayment) -> WeroPaymentInfo:
    if not payment.info_data.get("id"):
        return WeroPaymentInfo(
            id=None,
            last_polling=None,
            payment=None,
            consent=None,
            authorizations=None,
            capture=None,
            captures=None,
        )
    s = WeroPaymentInfoSerializer(data=payment.info_data)
    s.is_valid(raise_exception=True)
    return s.build()


@contextmanager
def payment_info(payment: OrderPayment, save: bool = False):
    info = load_payment_info_data(payment)
    try:
        yield info
    finally:
        payment.info_data = {**payment.info_data, **info.to_dict()}
        if save:
            payment.save(update_fields=["info"])


@dataclass
class WeroRefundInfo:
    id: Optional[UUID]
    last_polling: Optional[datetime]
    capture_id: Optional[UUID]
    refunds: Optional[List[Refund]]
    refund: Optional[List[CreatedRefund]]

    def to_dict(self) -> dict:
        return WeroRefundInfoSerializer(self).data


class WeroRefundInfoSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    last_polling = serializers.DateTimeField(required=False, allow_null=True)
    capture_id = serializers.UUIDField(allow_null=True, required=False)
    refunds = RefundSerializer(many=True, required=False, allow_null=True)
    refund = CreatedRefundSerializer(required=False, allow_null=True)

    @staticmethod
    def _is_legacy_refund_payload(data) -> bool:
        return (
            isinstance(data, dict)
            and "refund" not in data
            and "createdAt" in data
            and "status" in data
        )

    @staticmethod
    def _migrate_legacy_refund_payload(data: Dict) -> dict:
        return {"id": data["id"], "refund": data}

    def to_internal_value(self, data):
        if self._is_legacy_refund_payload(data):
            data = self._migrate_legacy_refund_payload(data)
        return super().to_internal_value(data)

    @staticmethod
    def to_domain(d) -> "WeroRefundInfo":
        refunds = d.get("refunds")
        return WeroRefundInfo(
            id=d["id"],
            last_polling=d.get("last_polling"),
            capture_id=d.get("capture_id"),
            refunds=(
                [RefundSerializer.to_domain(r) for r in refunds]
                if refunds is not None
                else None
            ),
            refund=d.get("refund"),
        )

    def build(self) -> "WeroRefundInfo":
        return self.to_domain(self.validated_data)


def load_refund_info_data(refund: OrderRefund) -> WeroRefundInfo:
    if not refund.info_data.get("id"):
        return WeroRefundInfo(
            id=None, last_polling=None, capture_id=None, refunds=None, refund=None
        )
    s = WeroRefundInfoSerializer(data=refund.info_data)
    s.is_valid(raise_exception=True)
    return s.build()


@contextmanager
def refund_info(refund: OrderRefund, save: bool = False):
    info = load_refund_info_data(refund)
    try:
        yield info
    finally:
        refund.info_data = {**refund.info_data, **info.to_dict()}
        if save:
            refund.save(update_fields=["info"])
