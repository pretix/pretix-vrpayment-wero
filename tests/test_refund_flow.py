import datetime
import json
import pytest
import uuid
from decimal import Decimal
from django.utils.timezone import now
from django_scopes import scope
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Organizer
from pretix.base.payment import PaymentException
from unittest.mock import MagicMock
from uuid import UUID

from pretix_vrpayment_wero.client import (
    Capture,
    CreatedRefund,
    GeneralError,
    Money,
    Refund,
    RefundType,
    Result,
    Status,
    WeroApiError,
    WeroError,
)
from pretix_vrpayment_wero.info_data import refund_info
from pretix_vrpayment_wero.payment import VRPaymentWero
from pretix_vrpayment_wero.tasks import refresh_refund

pytestmark = pytest.mark.django_db

LANDING_PAGE = "this_is_the_landing_page_url"


@pytest.fixture
def wero_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(
        "pretix_vrpayment_wero.payment.WeroClient",
        lambda *args, **kwargs: client,
    )
    return client


@pytest.fixture
def event():
    o = Organizer.objects.create(name="Dummy", slug="dummy")
    event: Event = Event.objects.create(
        organizer=o,
        name="Dummy",
        slug="dummy",
        date_from=now(),
        plugins="pretix_vrpayment_wero",
    )
    with scope(organizer=o):
        yield event


@pytest.fixture
def order(event) -> Order:
    return Order.objects.create(
        code="FOO",
        event=event,
        email="dummy@dummy.test",
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + datetime.timedelta(days=10),
        total=Decimal("46.00"),
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )


@pytest.fixture
def provider(event) -> VRPaymentWero:
    return VRPaymentWero(event)


@pytest.fixture
def payment(order, provider) -> OrderPayment:
    return OrderPayment.objects.create(
        order=order,
        amount=order.total,
        provider=provider.identifier,
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        info=json.dumps(
            {
                "id": str(uuid.uuid4()),
                "captures": [
                    Capture(
                        id=uuid.uuid4(),
                        authorization_id=uuid.uuid4(),
                        merchant_transaction_id=order.code,
                        payment=Money(amount=order.total, currency="EUR"),
                        status=Status(
                            result=Result.SUCCESSFUL, detail="", description=""
                        ),
                        created_at=now(),
                        settled_at=None,
                    ).to_dict()
                ],
            }
        ),
    )


@pytest.fixture
def order_refund(payment: OrderPayment) -> OrderRefund:
    return OrderRefund.objects.create(
        order=payment.order,
        payment=payment,
        amount=payment.amount,
        state=OrderRefund.REFUND_STATE_CREATED,
        source=OrderRefund.REFUND_SOURCE_BUYER,
        provider=payment.provider,
        info="{}",
    )


@pytest.fixture
def wero_refund_flow(wero_client, order) -> UUID:
    """Stub every WERO call with a successful response."""

    def successful() -> Status:
        return Status(result=Result.SUCCESSFUL, detail="REFUND_SETTLED", description="")

    money = Money(amount=order.total, currency="EUR")

    refund_id = uuid.uuid4()

    wero_client.create_refund.return_value = CreatedRefund(
        id=refund_id,
        created_at=now(),
        status=successful(),
    )
    wero_client.get_refunds.return_value = [
        Refund(
            id=refund_id,
            payment=money,
            type=RefundType.FULL,
            capture_id=order.payments.first().info_data["captures"][0]["id"],
            created_at=now(),
            status=successful(),
        )
    ]

    return refund_id


def test_execute_refund_successful(provider, order_refund, wero_refund_flow):
    provider.execute_refund(order_refund)

    order_refund.refresh_from_db()

    assert order_refund.state == OrderRefund.REFUND_STATE_DONE
    with refund_info(order_refund) as info_data:
        assert info_data.id == wero_refund_flow
        assert info_data.capture_id is not None
        assert info_data.refund is not None


@pytest.mark.parametrize(
    "side_effect",
    [
        (GeneralError,),
        (WeroApiError(wero_error=WeroError(), retry=False)),
        (WeroApiError(wero_error=WeroError(), retry=True)),
    ],
)
def test_execute_refund_unsuccessful_exception(
    provider, wero_client, order_refund, wero_refund_flow, side_effect
):
    wero_client.create_refund.side_effect = side_effect

    with pytest.raises(PaymentException):
        provider.execute_refund(order_refund)

    assert order_refund.state == OrderRefund.REFUND_STATE_FAILED


@pytest.mark.parametrize(
    "wero_state, refund_state",
    [
        (Result.PENDING, OrderRefund.REFUND_STATE_TRANSIT),
        (Result.FAILED, OrderRefund.REFUND_STATE_FAILED),
    ],
)
def test_execute_refund_unsuccessful(
    provider, wero_client, order_refund, wero_refund_flow, wero_state, refund_state
):
    wero_client.create_refund.return_value = CreatedRefund(
        id=wero_refund_flow,
        created_at=now(),
        status=Status(result=wero_state, detail="", description=""),
    )
    provider.execute_refund(order_refund)
    assert order_refund.state == refund_state


@pytest.mark.parametrize(
    "wero_state,wero_detail_state, refund_state",
    [
        (Result.PENDING, "", OrderRefund.REFUND_STATE_TRANSIT),
        (Result.FAILED, "", OrderRefund.REFUND_STATE_FAILED),
        (Result.SUCCESSFUL, "", OrderRefund.REFUND_STATE_TRANSIT),
        (Result.SUCCESSFUL, "REFUND_SETTLED", OrderRefund.REFUND_STATE_DONE),
    ],
)
def test_execute_refund_polling(
    provider,
    wero_client,
    order_refund,
    wero_refund_flow,
    wero_state,
    wero_detail_state,
    refund_state,
):
    wero_client.create_refund.return_value = CreatedRefund(
        id=wero_refund_flow,
        created_at=now(),
        status=Status(result=Result.PENDING, detail="", description=""),
    )
    wero_client.get_refunds.return_value = [
        Refund(
            id=wero_refund_flow,
            payment=Money(amount=order_refund.amount, currency="EUR"),
            type=RefundType.FULL,
            capture_id=UUID(order_refund.payment.info_data["captures"][0]["id"]),
            created_at=now(),
            status=Status(result=wero_state, detail=wero_detail_state, description=""),
        )
    ]

    provider.execute_refund(order_refund)

    refresh_refund(order_refund.id, False)
    order_refund.refresh_from_db()
    assert order_refund.state == refund_state


@pytest.mark.parametrize(
    "error, refund_state",
    [
        (GeneralError, OrderRefund.REFUND_STATE_TRANSIT),
        (
            WeroApiError(retry=True, wero_error=WeroError()),
            OrderRefund.REFUND_STATE_TRANSIT,
        ),
        (
            WeroApiError(retry=False, wero_error=WeroError()),
            OrderRefund.REFUND_STATE_FAILED,
        ),
    ],
)
def test_execute_refund_polling_errors(
    provider, wero_client, order_refund, wero_refund_flow, error, refund_state
):
    wero_client.create_refund.return_value = CreatedRefund(
        id=wero_refund_flow,
        created_at=now(),
        status=Status(result=Result.PENDING, detail="", description=""),
    )
    wero_client.get_refunds.side_effect = [error, error]
    provider.execute_refund(order_refund)

    refresh_refund(order_refund.id, False)
    order_refund.refresh_from_db()
    assert order_refund.state == refund_state
