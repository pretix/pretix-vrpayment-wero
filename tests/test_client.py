import os
import pytest
import requests
import time
import uuid
from decimal import Decimal

from pretix_vrpayment_wero.client import (
    GeneralError,
    Result,
    WeroClient,
)

WERO_TSP_CLIENT_ID = os.environ.get("WERO_TSP_CLIENT_ID", None)
WERO_TSP_CLIENT_SECRET = os.environ.get("WERO_TSP_CLIENT_SECRET", None)
TSP_MERCHANT_ID = os.environ.get("TSP_MERCHANT_ID", None)

WERO_CLIENT_ID = os.environ.get("WERO_CLIENT_ID", None)
WERO_CLIENT_SECRET = os.environ.get("WERO_CLIENT_SECRET", None)

WERO_UAT_API = "https://pay.werouat.eu/api"
CONSENTS_URL = "https://example-consumer-psps.werouat.eu/api/example-consumer-psps/wallets/44084ef4-ecd1-453f-869a-0158807c6424/payment-means/e7e6cbe6-e0a0-4198-b670-f5992280aa9a/consents/"  # noqa

requires_tsp_creds = pytest.mark.skipif(
    not WERO_TSP_CLIENT_ID or not WERO_TSP_CLIENT_SECRET or not TSP_MERCHANT_ID,
    reason="requires WERO_TSP_CLIENT_ID, WERO_TSP_CLIENT_SECRET and TSP_MERCHANT_ID vars",
)

requires_reg_creds = pytest.mark.skipif(
    not WERO_CLIENT_ID or not WERO_CLIENT_SECRET,
    reason="requires WERO_CLIENT_ID and WERO_CLIENT_SECRET vars",
)

CLIENT_PARAMS = [
    pytest.param("tsp", marks=requires_tsp_creds, id="tsp"),
    pytest.param("reg", marks=requires_reg_creds, id="reg"),
]


@pytest.fixture(params=CLIENT_PARAMS)
def client(request):
    kind = request.param
    if kind == "tsp":
        return WeroClient(
            WERO_TSP_CLIENT_ID,
            WERO_TSP_CLIENT_SECRET,
            WERO_TSP_CLIENT_ID,
            WERO_TSP_CLIENT_SECRET,
            True,
            True,
            TSP_MERCHANT_ID,
        )
    else:  # "reg"
        return WeroClient(
            WERO_CLIENT_ID,
            WERO_CLIENT_SECRET,
            WERO_CLIENT_ID,
            WERO_CLIENT_SECRET,
            True,
            False,
        )


def get_short_code(landing_page: str):
    consent_id = landing_page.split("/")[-1]

    resp = requests.get(f"{WERO_UAT_API}/consents/{consent_id}")
    resp.raise_for_status()
    body = resp.json()
    return body["shortId"]


def give_consent(short_id: str):
    resp = requests.put(
        f"{CONSENTS_URL}/{short_id}",
    )
    resp.raise_for_status()
    return


def test_auth_header(client):
    assert client.auth_header is not None
    assert "Authorization" in client.auth_header


def test_create_payment_and_refund(client):
    pretix_payment_id = f"pretix_test_{uuid.uuid4()}"

    payment = client.create_payment(
        pretix_payment_id,
        currency="EUR",
        amount=Decimal("1.00"),
        return_url="https://www.pretix.eu",
        description=pretix_payment_id,
    )

    # we haven't given consent yet - so it should be pending
    consent = client.get_consent(payment.id)
    assert consent.status.result == Result.PENDING

    # use the pay.werouat.eu endpoints to confirm the payment
    give_consent(get_short_code(payment.landing_page))

    consent = None
    # wait for successful consent
    for i in range(10):
        consent = client.get_consent(payment.id)
        if consent.status.result == Result.SUCCESSFUL:
            break
        time.sleep(1)

    assert consent is not None, "get_consent never succeeded"
    assert consent.status.result == Result.SUCCESSFUL

    # wait for successful authorization
    authorization = None
    for i in range(10):
        try:
            authorization = client.get_authorizations(payment.id)
        except GeneralError:
            continue
        if authorization[0].status.result == Result.SUCCESSFUL:
            break
        time.sleep(1)

    assert authorization is not None, "get_authorizations never succeeded"
    assert authorization[0].status.result == Result.SUCCESSFUL

    # create the capture
    client.create_capture(
        payment.id, pretix_payment_id, currency="EUR", amount=Decimal("1.00")
    )

    # read back capture state
    captures = None
    for _ in range(10):
        captures = client.get_captures(payment.id)
        if captures[0].status.result == Result.SUCCESSFUL:
            break
        time.sleep(1)

    assert captures is not None, "get_captures never succeeded"
    assert captures[0].status.result == Result.SUCCESSFUL

    # create the refund
    client.create_refund(
        payment.id,
        captures[0].id,
        amount=Decimal("1.00"),
        currency="EUR",
        description="refund",
    )

    # read back refund state
    refunds = None
    for _ in range(10):
        refunds = client.get_refunds(payment.id)
        if refunds[0].status.result == Result.SUCCESSFUL:
            break
        time.sleep(1)

    assert refunds is not None, "get_refunds never succeeded"
    assert refunds[0].status.result == Result.SUCCESSFUL
