import unittest
from datetime import datetime

from backend.models import Message
from backend import orchestrator as orch
from backend.store import ConversationStore


class _FakeStore:
    def __init__(self):
        self.citizens = {
            "ctz_test": {
                "language": "en-IN",
                "state_code": "TN",
                "msisdn": "9999999999",
            }
        }
        self.conversations = {"ctz_test:cmo": []}

    def get_citizen(self, citizen_id):
        return self.citizens.get(citizen_id)

    def set_citizen_language(self, citizen_id, lang):
        self.citizens[citizen_id]["language"] = lang

    def set_citizen_state(self, citizen_id, state_code, primary_language=""):
        self.citizens[citizen_id]["state_code"] = state_code
        if primary_language:
            self.citizens[citizen_id]["language"] = primary_language

    def append(self, msg):
        self.conversations.setdefault(msg.convId, []).append(msg)
        return msg

    def as_chat_messages(self, conv_id, limit=10):
        return [{"role": "user", "content": "hi"}]


class _NoopWS:
    async def send_to_citizen(self, citizen_id, frame):
        return None


class _NoopDispatcher:
    async def dispatch(self, citizen_id, frame, primary_channel="simulator"):
        return None


class _FakeLLM:
    async def chat_stream(self, messages):
        yield "Hello, how can I help?"


class OrchestratorTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_agent_turn_initializes_latency_state(self):
        original_store = orch.store
        original_ws = orch.ws_manager
        original_dispatcher = orch.dispatcher
        original_retrieve = orch.retrieve_with_meta
        original_get_llm_for = orch.get_llm_for
        original_record_turn = orch._lat.record_turn

        fake_store = _FakeStore()
        orch.store = fake_store
        orch.ws_manager = _NoopWS()
        orch.dispatcher = _NoopDispatcher()
        orch.retrieve_with_meta = lambda *args, **kwargs: []
        orch.get_llm_for = lambda provider=None: _FakeLLM()
        orch._lat.record_turn = lambda **kwargs: None

        try:
            await orch._run_agent_turn_impl(
                citizen_id="ctz_test",
                agent_id="cmo",
                conv_id="ctz_test:cmo",
                latest_user_text="hi",
            )
        finally:
            orch.store = original_store
            orch.ws_manager = original_ws
            orch.dispatcher = original_dispatcher
            orch.retrieve_with_meta = original_retrieve
            orch.get_llm_for = original_get_llm_for
            orch._lat.record_turn = original_record_turn

        messages = fake_store.conversations["ctz_test:cmo"]
        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[0], Message)
        self.assertEqual("agent", messages[0].role)
        self.assertEqual("Hello, how can I help?", messages[0].text)


class ConversationStoreOCRTests(unittest.TestCase):
    def test_ocr_tool_result_is_added_to_chat_context(self):
        store = ConversationStore.__new__(ConversationStore)
        store.history = lambda conv_id, limit=10: [
            Message(
                id="msg_user",
                convId=conv_id,
                role="user",
                type="media",
                text="[document: aadhaar.pdf]",
                timestamp=datetime.utcnow(),
            ),
            Message(
                id="msg_ocr",
                convId=conv_id,
                role="system",
                type="tool_result",
                text="[vision] aadhaar",
                timestamp=datetime.utcnow(),
                extra={
                    "toolId": "vision.extract_document",
                    "result": {
                        "document_type": "aadhaar",
                        "confidence": 0.97,
                        "language": "en-IN",
                        "document": {"name": "Citizen", "aadhaar_number": "123456789012"},
                        "raw_text": "Citizen Aadhaar card",
                    },
                },
            ),
        ]

        msgs = ConversationStore.as_chat_messages(store, "ctz_1:cmo")
        self.assertEqual(2, len(msgs))
        self.assertEqual("user", msgs[0]["role"])
        self.assertEqual("system", msgs[1]["role"])
        self.assertIn("OCR result from uploaded document", msgs[1]["content"])
        self.assertIn("aadhaar_number", msgs[1]["content"])


class OCRFollowupPromptTests(unittest.TestCase):
    def test_generic_ocr_prompt_tells_agent_not_to_guess_document_type(self):
        class _OCR:
            document_type = "document"
            fields = {}
            confidence = 0.8
            raw_text = ""

        prompt = orch._ocr_followup_prompt(_OCR())
        self.assertIn("could not confidently identify the document type", prompt)
        self.assertIn("Do NOT guess that it is Aadhaar", prompt)
        self.assertIn("re-upload a clearer photo", prompt)


class VisionHeuristicTests(unittest.TestCase):
    def test_language_hint_normalizes_odia_to_sarvam_code(self):
        from backend.vision import _normalize_vision_language

        self.assertEqual("or-IN", _normalize_vision_language("od-IN"))
        self.assertEqual("hi-IN", _normalize_vision_language(""))

    def test_document_type_guess_uses_filename_and_text(self):
        from backend.vision import _guess_document_type

        aadhaar_text = "Government of India\nUnique Identification Authority of India\nAadhaar"
        self.assertEqual(
            "aadhaar",
            _guess_document_type(aadhaar_text, "scan.png", None),
        )
        self.assertEqual(
            "pan",
            _guess_document_type("Permanent Account Number", "pan_card.jpg", "auto"),
        )


if __name__ == "__main__":
    unittest.main()
