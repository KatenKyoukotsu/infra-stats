import asyncio
import unittest
from urllib.parse import parse_qs

import httpx

from app.vmclient.vmclient import TokenBucket, VmClient, _next_backoff, _parse_value


def _query_of(request) -> str:
    return parse_qs(request.content.decode()).get("query", [""])[0]


class TokenBucketTest(unittest.IsolatedAsyncioTestCase):
    async def test_burst_allows_first_requests(self):
        bucket = TokenBucket(rate=1.0, burst=3.0)
        # 3 токена из burst — проходят без задержки
        for _ in range(3):
            await asyncio.wait_for(bucket.wait(), 0.2)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(bucket.wait(), 0.2)


class ParseValueTest(unittest.TestCase):
    def test_parses_string_value(self):
        self.assertEqual(_parse_value({"value": [123, "45.6"]}), 45.6)

    def test_parses_nan(self):
        self.assertTrue(_parse_value({"value": [123, "NaN"]}) != _parse_value({"value": [123, "NaN"]}))

    def test_missing_value(self):
        self.assertIsNone(_parse_value({"value": []}))
        self.assertIsNone(_parse_value({}))

    def test_bad_value(self):
        self.assertIsNone(_parse_value({"value": [123, "abc"]}))


class NextBackoffTest(unittest.TestCase):
    def test_honors_retry_after(self):
        resp = httpx.Response(503, headers={"Retry-After": "4"})
        self.assertEqual(_next_backoff(0.3, resp), 4.0)

    def test_retry_after_capped(self):
        resp = httpx.Response(503, headers={"Retry-After": "999"})
        self.assertEqual(_next_backoff(0.3, resp), 5.0)

    def test_503_longer_backoff(self):
        resp = httpx.Response(503)
        self.assertEqual(_next_backoff(0.3, resp), 2.0)

    def test_generic_retryable(self):
        resp = httpx.Response(500)
        self.assertEqual(_next_backoff(0.3, resp), 0.6)


class RetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_on_500_then_succeeds(self):
        calls = []

        async def handler(request):
            self.assertEqual(request.method, "POST")
            calls.append(_query_of(request))
            if len(calls) < 2:
                return httpx.Response(500, text="boom")
            return httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": []}},
            )

        transport = httpx.MockTransport(handler)
        client = VmClient("http://vm", timeout=1.0, max_concurrent=8, rps=1000.0, retries=3)
        client._http = httpx.AsyncClient(transport=transport, timeout=1.0)
        try:
            result = await client.query("up{}")
            self.assertEqual(result, [])
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], "up{}")
        finally:
            await client.close()

    async def test_raises_after_exhausting_retries(self):
        calls = []

        async def handler(request):
            calls.append(1)
            return httpx.Response(503, text="slow")

        transport = httpx.MockTransport(handler)
        client = VmClient("http://vm", timeout=1.0, max_concurrent=8, rps=1000.0, retries=1)
        client._http = httpx.AsyncClient(transport=transport, timeout=1.0)
        try:
            with self.assertRaises(RuntimeError):
                await client.query("up{}")
            self.assertEqual(len(calls), 2)  # 1 исходный + 1 ретрай
        finally:
            await client.close()


class SeriesParseTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_returns_series(self):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {"metric": {"instance": "a:9100"}, "value": [123, "42.5"]},
                            {"metric": {"instance": "b:9100"}, "value": [123, "NaN"]},
                        ],
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        client = VmClient("http://vm", timeout=1.0, max_concurrent=8, rps=1000.0, retries=0)
        client._http = httpx.AsyncClient(transport=transport, timeout=1.0)
        try:
            series = await client.query("up{}")
            # Клиент возвращает и NaN-серию (как в Go); фильтрует уже анализатор.
            self.assertEqual(len(series), 2)
            self.assertEqual(series[0].metric["instance"], "a:9100")
            self.assertEqual(series[0].value, 42.5)
            self.assertNotEqual(series[1].value, series[1].value)  # NaN
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
