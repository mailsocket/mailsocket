"Public API."

from django.conf import settings

PLAN_VARIANT_INTERVAL = ("month", 1)


def variant_interval(variant_id):
    """(interval, interval_count) for a plan variant id, else None."""
    plan_variants = {settings.LS_VARIANT_PAID, settings.LS_VARIANT_SCALE, settings.LS_VARIANT_BUSINESS}
    if variant_id and str(variant_id) in plan_variants:
        return PLAN_VARIANT_INTERVAL
    return None


def refresh_period_start(plan, variant_id):
    "Public API."
    from apps.billing.views import billing_period_start  # views imports this module

    interval = variant_interval(variant_id)
    if interval is None or plan.current_period_end is None:
        return False
    plan.current_period_start = billing_period_start(plan.current_period_end, *interval)
    return True
