"""
Custom LiteLLM callback that emits org-attribution dimensions alongside the
built-in 'otel' callback.

LiteLLM's built-in OTEL integration only promotes a fixed set of span
attributes (user_id, user_email, team_id, model). Org fields stored in API key
metadata (department, team, cost_center, organization, location, role) are
silently dropped. This callback emits a per-request OTEL counter with those
fields as datapoint labels so the OTEL collector can lift them to CloudWatch
dimensions via the 'attributes' processor.

LiteLLM discovers this file via litellm_config.yaml:
  litellm_settings:
    callbacks: ["otel", "./otel_org_callback.py"]

The global `my_custom_logger` variable is the instance LiteLLM loads.
"""
import logging
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger(__name__)

_METRIC_NAME = "litellm.request.count"
_METER_NAME = "litellm.org"
_ORG_FIELDS = ("department", "team", "cost_center", "organization", "location", "role", "manager")


class OrgAttributeCallback(CustomLogger):
    """
    Emits litellm.request.count with full org context as datapoint labels.
    Uses lazy OTEL meter initialization so the global MeterProvider (set up by
    LiteLLM's own 'otel' callback) is already configured when first used.
    """

    def __init__(self):
        self._counter = None

    def _counter_or_none(self):
        if self._counter is not None:
            return self._counter
        try:
            from opentelemetry import metrics as otel_metrics
            meter = otel_metrics.get_meter(_METER_NAME)
            self._counter = meter.create_counter(
                _METRIC_NAME,
                description="LiteLLM request count with org attribution",
            )
        except Exception as exc:
            logger.warning("OrgAttributeCallback: could not create OTEL counter: %s", exc)
        return self._counter

    def _attributes(self, kwargs: dict) -> dict:
        metadata = ((kwargs.get("litellm_params") or {}).get("metadata") or {})
        return {
            "user_email": metadata.get("email", ""),
            "user_id": kwargs.get("user", ""),
            "model": kwargs.get("model", ""),
            **{field: metadata.get(field, "") for field in _ORG_FIELDS},
        }

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            counter = self._counter_or_none()
            if counter is not None:
                counter.add(1, self._attributes(kwargs))
        except Exception as exc:
            logger.debug("OrgAttributeCallback: %s", exc)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)


my_custom_logger = OrgAttributeCallback()
