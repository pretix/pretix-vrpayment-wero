from django.utils.translation import gettext_lazy as _

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_vrpayment_wero"
    verbose_name = "VR Payment (WERO)"

    class PretixPluginMeta:
        name = _("VR Payment (WERO)")
        author = "pretix Team"
        description = _(
            "Accept WERO payments through VR Payment, the payment service provider of Volksbanken Raiffeisenbanken."
        )
        visible = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=2.7.0"
        picture = "pretix_vrpayment_wero/logo.svg"
        settings_links = [
            (
                (_("Payment"), _("WERO (VR Payment)")),
                "control:event.settings.payment.provider",
                {"provider": "vrpayment_wero"},
            ),
        ]
        navigation_links = []

    def ready(self):
        from . import signals  # NOQA
