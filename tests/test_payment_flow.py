import datetime
import pytest
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from django.utils.timezone import now
from django_scopes import scope
from pretix.base.models import Event, Order, OrderPayment, Organizer
from pretix.base.payment import PaymentException
from unittest.mock import MagicMock

from pretix_vrpayment_wero.client import (
    Authorization,
    Capture,
    Consent,
    CreatedCapture,
    CreatedPayment,
    GeneralError,
    Money,
    Result,
    Status,
    WeroApiError,
    WeroError,
)
from pretix_vrpayment_wero.info_data import payment_info
from pretix_vrpayment_wero.payment import VRPaymentWero
from pretix_vrpayment_wero.tasks import refresh_transaction

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
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )


@dataclass
class WeroFlowIds:
    payment_id: uuid.UUID = field(default_factory=uuid.uuid4)
    consent_id: uuid.UUID = field(default_factory=uuid.uuid4)
    authorization_id: uuid.UUID = field(default_factory=uuid.uuid4)
    capture_id: uuid.UUID = field(default_factory=uuid.uuid4)


@pytest.fixture
def wero_flow(wero_client, order) -> WeroFlowIds:
    """Stub every WERO call with a successful response."""

    def successful() -> Status:
        return Status(result=Result.SUCCESSFUL, detail="", description="")

    flow = WeroFlowIds()
    money = Money(amount=order.total, currency="EUR")

    wero_client.create_payment.return_value = CreatedPayment(
        id=flow.payment_id,
        landing_page=LANDING_PAGE,
    )
    wero_client.get_consent.return_value = Consent(
        id=flow.consent_id,
        payment=money,
        created_at=now(),
        status=successful(),
    )
    wero_client.get_authorizations.return_value = [
        Authorization(
            id=flow.authorization_id,
            payment=money,
            created_at=now(),
            status=successful(),
        )
    ]

    wero_client.create_capture.return_value = CreatedCapture(
        id=flow.capture_id,
        created_at=now(),
        status=successful(),
    )
    wero_client.get_captures.return_value = [
        Capture(
            id=flow.capture_id,
            authorization_id=flow.authorization_id,
            merchant_transaction_id="",
            settled_at=now(),
            payment=money,
            created_at=now(),
            status=successful(),
        )
    ]

    return flow


@pytest.fixture
def started_payment(provider, payment, wero_flow) -> OrderPayment:
    """
    A payment that has been handed off to WERO and is waiting to be polled.
    """
    provider.execute_payment({}, payment)
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
    return payment


def test_execute_payment_successful(provider, payment, wero_flow):
    provider.execute_payment({}, payment)

    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    with payment_info(payment) as info_data:
        assert info_data.id == wero_flow.payment_id
        assert info_data.payment.id == wero_flow.payment_id
        assert info_data.payment.landing_page == LANDING_PAGE


@pytest.mark.parametrize(
    "side_effect",
    [(GeneralError,), (WeroApiError(retry=False, wero_error=WeroError()))],
)
def test_execute_payment_general_error(provider, payment, wero_client, side_effect):
    wero_client.create_payment.side_effect = side_effect

    with pytest.raises(PaymentException):
        provider.execute_payment({}, payment)

    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


def test_polling_confirms_payment(started_payment):
    refresh_transaction(started_payment.id, False)
    started_payment.refresh_from_db()
    assert started_payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


# info_data keys a poll has already recorded by the time each step runs
INFO_RECORDED_BEFORE_STEP = {
    "get_consent": [],
    "get_authorizations": ["consent"],
    "create_capture": ["consent", "authorizations"],
    "get_captures": ["consent", "authorizations", "capture"],
}

POLLING_STEPS = list(INFO_RECORDED_BEFORE_STEP)


@pytest.mark.parametrize(
    "failing_step,expected_state",
    [
        ("get_consent", OrderPayment.PAYMENT_STATE_PENDING),
        ("get_authorizations", OrderPayment.PAYMENT_STATE_PENDING),
        ("create_capture", OrderPayment.PAYMENT_STATE_PENDING),
        ("get_captures", OrderPayment.PAYMENT_STATE_PENDING),
    ],
)
def test_general_error_leaves_payment_open(
    started_payment, wero_client, failing_step, expected_state
):
    with payment_info(started_payment) as info_data:
        previous_polling = info_data.last_polling
    getattr(wero_client, failing_step).side_effect = GeneralError

    refresh_transaction(started_payment.id, False)

    started_payment.refresh_from_db()

    assert started_payment.state == expected_state
    with payment_info(started_payment) as info_data:
        assert info_data.last_polling != previous_polling
        previous_polling = info_data.last_polling
        for key in INFO_RECORDED_BEFORE_STEP[failing_step]:
            assert getattr(info_data, key) is not None

    # the next poll, with the error gone, picks up where this one left off
    getattr(wero_client, failing_step).side_effect = None

    refresh_transaction(started_payment.id, False)

    started_payment.refresh_from_db()
    assert started_payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    with payment_info(started_payment) as info_data:
        assert info_data.last_polling != previous_polling
        assert info_data.captures is not None


@pytest.mark.parametrize("failing_step", POLLING_STEPS)
def test_wero_api_error_fails_payment(started_payment, wero_client, failing_step):
    getattr(wero_client, failing_step).side_effect = WeroApiError(
        retry=False, wero_error=WeroError()
    )

    refresh_transaction(started_payment.id, False)

    started_payment.refresh_from_db()
    assert started_payment.state == OrderPayment.PAYMENT_STATE_FAILED
    for key in INFO_RECORDED_BEFORE_STEP[failing_step]:
        assert started_payment.info_data.get(key) is not None, key


@pytest.fixture
def set_step_status(wero_client, wero_flow, order):
    money = Money(amount=order.total, currency="EUR")

    def _set(step: str, result: Result) -> None:
        status = Status(result=result, detail="", description="")

        if step == "get_consent":
            wero_client.get_consent.return_value = Consent(
                id=wero_flow.consent_id,
                payment=money,
                created_at=now(),
                status=status,
            )
        elif step == "get_authorizations":
            wero_client.get_authorizations.return_value = [
                Authorization(
                    id=wero_flow.authorization_id,
                    payment=money,
                    created_at=now(),
                    status=status,
                )
            ]

        elif step == "create_capture":
            wero_client.create_capture.return_value = CreatedCapture(
                id=wero_flow.capture_id,
                created_at=now(),
                status=status,
            )
        elif step == "get_captures":
            wero_client.get_captures.return_value = [
                Capture(
                    id=wero_flow.capture_id,
                    authorization_id=wero_flow.authorization_id,
                    merchant_transaction_id="",
                    settled_at=now(),
                    payment=money,
                    created_at=now(),
                    status=status,
                )
            ]

        else:
            raise ValueError(step)

    return _set


@pytest.mark.parametrize(
    "step,wero_state,expected_state",
    [
        ("get_consent", Result.PENDING, OrderPayment.PAYMENT_STATE_PENDING),
        ("get_consent", Result.FAILED, OrderPayment.PAYMENT_STATE_FAILED),
        ("get_authorizations", Result.PENDING, OrderPayment.PAYMENT_STATE_PENDING),
        ("get_authorizations", Result.FAILED, OrderPayment.PAYMENT_STATE_FAILED),
        ("get_captures", Result.PENDING, OrderPayment.PAYMENT_STATE_PENDING),
        ("get_captures", Result.FAILED, OrderPayment.PAYMENT_STATE_FAILED),
    ],
)
def test_polling_non_successful_status(
    started_payment, set_step_status, step, wero_state, expected_state
):
    set_step_status(step, wero_state)

    refresh_transaction(started_payment.id, False)

    started_payment.refresh_from_db()
    assert started_payment.state == expected_state


@pytest.mark.parametrize("step", ["get_consent", "get_authorizations", "get_captures"])
def test_pending_step_confirms_on_later_poll(started_payment, set_step_status, step):
    set_step_status(step, Result.PENDING)

    refresh_transaction(started_payment.id, False)
    started_payment.refresh_from_db()
    assert started_payment.state in (
        OrderPayment.PAYMENT_STATE_CREATED,
        OrderPayment.PAYMENT_STATE_PENDING,
    )

    # WERO has moved on; the next poll picks it up
    set_step_status(step, Result.SUCCESSFUL)

    refresh_transaction(started_payment.id, False)
    started_payment.refresh_from_db()
    assert started_payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert started_payment.info_data.get("captures") is not None
