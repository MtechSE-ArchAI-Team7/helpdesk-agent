import unittest

from helpdesk_agent.domain.models.conversation import Conversation
from helpdesk_agent.infrastructure.persistence.conversations import ConversationStore


class TestConversation(unittest.TestCase):
    def test_create_conversation(self) -> None:
        conv = Conversation()
        self.assertIsNotNone(conv.conversation_id)
        self.assertEqual(len(conv.messages), 0)
        self.assertIsNotNone(conv.created_at)

    def test_add_user_message(self) -> None:
        conv = Conversation()
        conv.add_user_message("Hello, help with my issue")
        self.assertEqual(len(conv.messages), 1)
        self.assertEqual(conv.messages[0]["role"], "user")
        self.assertEqual(conv.messages[0]["content"], "Hello, help with my issue")

    def test_add_agent_message(self) -> None:
        conv = Conversation()
        conv.add_agent_message("I can help with that", {"case_id": "case-123"})
        self.assertEqual(len(conv.messages), 1)
        self.assertEqual(conv.messages[0]["role"], "agent")
        self.assertEqual(conv.messages[0]["content"], "I can help with that")
        self.assertEqual(conv.messages[0]["metadata"]["case_id"], "case-123")

    def test_conversation_to_dict(self) -> None:
        conv = Conversation("test-id")
        conv.add_user_message("test message")
        data = conv.to_dict()
        self.assertEqual(data["conversation_id"], "test-id")
        self.assertEqual(len(data["messages"]), 1)
        self.assertIn("created_at", data)


class TestConversationStore(unittest.TestCase):
    def test_create_conversation(self) -> None:
        store = ConversationStore()
        conv = store.create()
        self.assertIn(conv.conversation_id, store.conversations)

    def test_get_conversation(self) -> None:
        store = ConversationStore()
        store.create("test-id")
        retrieved = store.get("test-id")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.conversation_id, "test-id")

    def test_get_nonexistent_conversation(self) -> None:
        store = ConversationStore()
        retrieved = store.get("nonexistent")
        self.assertIsNone(retrieved)

    def test_get_or_create_existing(self) -> None:
        store = ConversationStore()
        conv = store.create("test-id")
        conv.add_user_message("test")
        retrieved = store.get_or_create("test-id")
        self.assertEqual(len(retrieved.messages), 1)

    def test_get_or_create_new(self) -> None:
        store = ConversationStore()
        conv = store.get_or_create("new-id")
        self.assertEqual(conv.conversation_id, "new-id")
        self.assertIn("new-id", store.conversations)

    def test_delete_conversation(self) -> None:
        store = ConversationStore()
        store.create("test-id")
        result = store.delete("test-id")
        self.assertTrue(result)
        self.assertNotIn("test-id", store.conversations)

    def test_list_all(self) -> None:
        store = ConversationStore()
        store.create("id1")
        store.create("id2")
        convs = store.list_all()
        self.assertEqual(len(convs), 2)


if __name__ == "__main__":
    unittest.main()
