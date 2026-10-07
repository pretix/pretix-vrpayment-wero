from pretix_vrpayment_wero.info_data import (
    WeroPaymentInfoSerializer,
    WeroRefundInfoSerializer,
)

OLD_PAYMENT_INFO_DATA = {
    "authorizations": [
        {
            "createdAt": "2026-09-07T10:17:15.054615Z",
            "id": "80019071-446a-4c56-90f8-dfcffd298d74",
            "payment": {"amount": 10, "currency": "EUR"},
            "status": {
                "code": "000.000.000",
                "description": "Authorization is granted by the issuer.",
                "detail": "AUTHORIZATION_GRANTED",
                "result": "SUCCESSFUL",
            },
        }
    ],
    "captures": [
        {
            "authorizationId": "80019071-446a-4c56-90f8-dfcffd298d74",
            "createdAt": "2026-09-07T10:17:17.873237Z",
            "id": "4cbd75ae-f92b-4033-ab5f-796360381a34",
            "merchantTransactionId": "KONZERT-W0HCB-P-1",
            "payment": {"amount": 10, "currency": "EUR"},
            "settledAt": "2026-09-07T10:17:19.383382Z",
            "status": {
                "code": "000.000.000",
                "description": "Capture is settled.",
                "detail": "CAPTURE_SETTLED",
                "result": "SUCCESSFUL",
            },
        }
    ],
    "consent": {
        "createdAt": "2026-09-07T10:16:05.533359Z",
        "id": "95292359-1919-4bce-977e-65f04830a02f",
        "payment": {"amount": 10, "currency": "EUR"},
        "status": {
            "code": "000.000.000",
            "description": "Consent is granted by the consumer.",
            "detail": "CONSENT_GRANTED",
            "result": "SUCCESSFUL",
        },
    },
    "id": "9eda241f-c745-4e2c-8418-1d608be32b9d",
    "landingPage": "https://pay.werouat.eu/01a07b5e-3060-7e0e-9437-da35a492251e",
    "last_polling": "2026-09-07T10:17:19.203816+00:00",
}


def test_payment_backwards_compatibility():
    s = WeroPaymentInfoSerializer(data=OLD_PAYMENT_INFO_DATA)
    s.is_valid(raise_exception=True)
    validated_data = s.build()

    assert validated_data.id is not None
    assert str(validated_data.id) == OLD_PAYMENT_INFO_DATA["id"]

    assert validated_data.last_polling is not None
    assert (
        validated_data.last_polling.isoformat() == OLD_PAYMENT_INFO_DATA["last_polling"]
    )

    assert validated_data.consent is not None
    assert validated_data.consent.id is not None

    assert validated_data.captures is not None
    assert validated_data.captures[0].id is not None

    assert validated_data.authorizations is not None
    assert validated_data.authorizations[0].id is not None


OLD_REFUND_INFO_DATA = {
    "createdAt": "2026-09-14T12:10:46.646156Z",
    "id": "05e69a9b-d9ae-4632-b2fe-f2cf778c6e82",
    "status": {
        "code": "000.000.000",
        "description": "Refund is accepted. Processing is ongoing.",
        "detail": "REFUND_PENDING",
        "result": "SUCCESSFUL",
    },
}


def test_payment_backward_compatibility():
    s = WeroRefundInfoSerializer(data=OLD_REFUND_INFO_DATA)
    s.is_valid(raise_exception=True)
    validated_data = s.build()

    assert validated_data.id is not None
    assert str(validated_data.id) == OLD_REFUND_INFO_DATA["id"]

    assert validated_data.refund is not None


def test_empty_detail():
    s = WeroRefundInfoSerializer(
        data={
            "createdAt": "2026-03-16T09:23:56.701057Z",
            "id": "00000000-0000-0000-0000-000000000000",
            "status": {
                "code": "000.000.000",
                "description": "Transaction succeeded",
                "result": "SUCCESSFUL",
            },
        }
    )
    s.is_valid(raise_exception=True)

    s = WeroPaymentInfoSerializer(
        data={
            "authorizations": [
                {
                    "createdAt": "2026-09-07T10:17:15.054615Z",
                    "id": "80019071-446a-4c56-90f8-dfcffd298d74",
                    "payment": {"amount": 10, "currency": "EUR"},
                    "status": {
                        "code": "000.000.000",
                        "description": "Authorization is granted by the issuer.",
                        "result": "SUCCESSFUL",
                    },
                }
            ],
            "captures": [
                {
                    "authorizationId": "80019071-446a-4c56-90f8-dfcffd298d74",
                    "createdAt": "2026-09-07T10:17:17.873237Z",
                    "id": "4cbd75ae-f92b-4033-ab5f-796360381a34",
                    "merchantTransactionId": "KONZERT-W0HCB-P-1",
                    "payment": {"amount": 10, "currency": "EUR"},
                    "settledAt": "2026-09-07T10:17:19.383382Z",
                    "status": {
                        "code": "000.000.000",
                        "description": "Capture is settled.",
                        "result": "SUCCESSFUL",
                    },
                }
            ],
            "consent": {
                "createdAt": "2026-09-07T10:16:05.533359Z",
                "id": "95292359-1919-4bce-977e-65f04830a02f",
                "payment": {"amount": 10, "currency": "EUR"},
                "status": {
                    "code": "000.000.000",
                    "description": "Consent is granted by the consumer.",
                    "result": "SUCCESSFUL",
                },
            },
            "id": "9eda241f-c745-4e2c-8418-1d608be32b9d",
            "landingPage": "https://pay.werouat.eu/01a07b5e-3060-7e0e-9437-da35a492251e",
            "last_polling": "2026-09-07T10:17:19.203816+00:00",
        }
    )
    s.is_valid(raise_exception=True)
