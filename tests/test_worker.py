import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.worker import process_job


class FakeQueue:
    def __init__(self):
        self.completed = []
        self.retries = []

    async def complete(self, job_id, lease_token, result):
        self.completed.append((job_id, lease_token, result))
        return True

    async def retry(self, job_id, lease_token, delay, code):
        self.retries.append((job_id, lease_token, delay, code))
        return "retrying"


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_job_persists_answer_and_provider_metadata(self):
        queue = FakeQueue()
        job = {
            "id": "job-1",
            "lease_token": "lease-1",
            "attempts": 1,
            "payload": {"prompt": "Résous 2x = 8", "category": "raisonnement"},
        }
        with patch("app.router.pick_and_call", new=AsyncMock(
            return_value=("groq", "model-x", "x = 4")
        )):
            await process_job(queue, job)

        self.assertEqual(len(queue.completed), 1)
        self.assertEqual(queue.completed[0][0:2], ("job-1", "lease-1"))
        self.assertEqual(queue.completed[0][2]["response"], "x = 4")
        self.assertEqual(queue.completed[0][2]["provider"], "groq")
        self.assertEqual(queue.retries, [])


    async def test_math_answer_is_verified_and_corrected_before_completion(self):
        queue = FakeQueue()
        job = {
            "id": "job-math",
            "lease_token": "lease-math",
            "attempts": 1,
            "payload": {
                "prompt": "Résous 5 + 7.",
                "category": "general",
                "verify_math": True,
            },
        }
        with (
            patch(
                "app.router.pick_and_call",
                new=AsyncMock(side_effect=[
                    ("groq", "model-a", "5 + 7 = 13"),
                    ("gemini", "model-b", "5 + 7 = 12"),
                ]),
            ) as call_model,
            patch(
                "app.worker.verify_math_answer",
                side_effect=[["Ligne 1 : égalité fausse"], []],
            ) as verify,
        ):
            await process_job(queue, job)

        self.assertEqual(call_model.await_count, 2)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(queue.completed[0][2]["response"], "5 + 7 = 12")
        self.assertEqual(queue.completed[0][2]["provider"], "gemini")
        self.assertEqual(queue.retries, [])


    async def test_unresolved_math_error_is_retried_not_saved_as_answer(self):
        queue = FakeQueue()
        job = {
            "id": "job-math-bad",
            "lease_token": "lease-math-bad",
            "attempts": 1,
            "payload": {
                "prompt": "Étudie la fonction rationnelle.",
                "category": "general",
                "verify_math": True,
            },
        }
        with (
            patch(
                "app.router.pick_and_call",
                new=AsyncMock(side_effect=[
                    ("groq", "model-a", "Réponse incohérente"),
                    ("gemini", "model-b", "Toujours incohérente"),
                ]),
            ),
            patch(
                "app.worker.verify_math_answer",
                side_effect=[["erreur initiale"], ["erreur persistante"]],
            ),
        ):
            await process_job(queue, job)

        self.assertEqual(queue.completed, [])
        self.assertEqual(queue.retries[0][3], "math_verification_failed")
        self.assertGreaterEqual(queue.retries[0][2], 30)

    async def test_invalid_payload_is_retried_without_provider_call(self):
        queue = FakeQueue()
        job = {
            "id": "job-2",
            "lease_token": "lease-2",
            "attempts": 1,
            "payload": {"prompt": "  "},
        }
        await process_job(queue, job)
        self.assertEqual(queue.retries, [("job-2", "lease-2", 1, "invalid_job_payload")])
        self.assertEqual(queue.completed, [])


if __name__ == "__main__":
    unittest.main()
