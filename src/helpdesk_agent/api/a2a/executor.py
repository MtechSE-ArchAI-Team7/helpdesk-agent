"""A2A executor bridging Agent2Agent JSON-RPC tasks to the helpdesk workflow."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, TextPart
from a2a.utils import new_task

from helpdesk_agent.application.chat import handle_chat_message
from helpdesk_agent.infrastructure.persistence.conversations import ConversationStore

if TYPE_CHECKING:
    from helpdesk_agent.workflows.workflow import HelpdeskWorkflow

logger = logging.getLogger(__name__)


class HelpdeskAgentExecutor(AgentExecutor):
    """Executes A2A ``message/send`` requests via the shared chat orchestration.

    The A2A ``contextId`` maps 1:1 to a chat ``conversation_id``, so follow-up
    messages on the same context resume the same conversation — including the
    clarification flow, which surfaces as an ``input-required`` task state.
    """

    def __init__(self, workflow: HelpdeskWorkflow, conversation_store: ConversationStore) -> None:
        self._workflow = workflow
        self._conversation_store = conversation_store

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            if context.message is None:
                raise ValueError("A2A request context carries no message")
            task = new_task(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()

        try:
            response = await handle_chat_message(
                self._workflow,
                self._conversation_store,
                context.get_user_input(),
                task.context_id,
                source="a2a",
                sender="a2a-agent",
            )

            reply = updater.new_agent_message(
                [Part(root=TextPart(text=response.agent_response))],
                metadata={
                    "case_id": response.case_id,
                    "current_step": response.metadata.get("current_step"),
                },
            )

            if response.metadata.get("current_step") == "awaiting_clarification":
                # Multi-turn: the next message on the same contextId resumes the
                # conversation. final=True is required for non-streaming send.
                await updater.requires_input(reply, final=True)
            else:
                await updater.add_artifact(
                    [Part(root=TextPart(text=response.model_dump_json()))],
                    name="helpdesk-result",
                )
                await updater.complete(reply)
        except Exception as exc:  # noqa: BLE001 - task boundary must surface failure
            logger.exception("A2A task failed: context_id=%s", task.context_id)
            await updater.failed(updater.new_agent_message([Part(root=TextPart(text=f"helpdesk agent error: {exc}"))]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Best-effort cancellation; in-process runs complete synchronously."""
        task = context.current_task
        if task is None:
            logger.warning("cancel requested but no active task in request context")
            return
        await TaskUpdater(event_queue, task.id, task.context_id).cancel()
