"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.application.diagnosis import _TIME_WINDOW_LOOKBACK as _TIME_WINDOW_LOOKBACK
from helpdesk_agent.application.diagnosis import build_case_input as build_case_input
from helpdesk_agent.application.diagnosis import current_trace_id as current_trace_id
from helpdesk_agent.infrastructure.a2a.envelopes import build_send_message_envelope as build_send_message_envelope
from helpdesk_agent.infrastructure.a2a.envelopes import envelope_digest as envelope_digest
