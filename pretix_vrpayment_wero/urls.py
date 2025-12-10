from django.urls import include, path, re_path

from .views import ReturnView


def get_event_patterns(brand):
    return [
        re_path(
            r"^(?P<payment_provider>{})/".format(brand),
            include(
                [
                    path(
                        "return/<str:order>/<str:hash>/<int:payment>/",
                        ReturnView.as_view(),
                        name="return",
                    ),
                ]
            ),
        ),
    ]


event_patterns = get_event_patterns("vrpayment_wero")
