"""Legacy workflow construction and imports."""

from helpdesk_agent.api.factory import HelpdeskWorkflow as HelpdeskWorkflow
from helpdesk_agent.observability.events import _record_gate_decision as _record_gate_decision
from helpdesk_agent.observability.workflow import _trace_step as _trace_step
from helpdesk_agent.workflows.state import GraphState as GraphState
from helpdesk_agent.workflows.workflow import CLARIFICATION_COMMENT_MARKER as CLARIFICATION_COMMENT_MARKER
from helpdesk_agent.workflows.workflow import DIAGNOSIS_AGENT_NAME as DIAGNOSIS_AGENT_NAME
