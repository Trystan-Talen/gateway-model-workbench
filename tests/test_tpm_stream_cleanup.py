"""Owned stream cleanup preserves HTTPX semantics and charged failure evidence."""
import asyncio
from contextlib import asynccontextmanager
import gc
import json
import time
import unittest
from unittest.mock import patch
import weakref

import httpx

from tpm_engine import ReleasingResponseStream, TpmRun, consume_sse
from tpm_usage import normalize_usage


def config(**updates):
    value = dict(url='https://mock.invalid/v1', model='synthetic', api_key='synthetic-only',
                 input_tokens=128, expected_output_tokens=4, max_output_tokens=16,
                 calibration_requests=2, template_groups=1, warmup_requests=0,
                 context_window_tokens=256, protocol_overhead_tokens=0,
                 cache_write_price_mode='input', cache_mode='avoid_cache',
                 concurrency=16, pool_connections=16, budget_usd=5,
                 target_tpm=132 * 60 * 25, target_duration_seconds=.12,
                 ramp_duration_seconds=.05, timeout_seconds=.4)
    value.update(updates)
    return value


USAGE = {'prompt_tokens': 128, 'completion_tokens': 4,
         'prompt_tokens_details': {'cached_tokens': 0}, 'total_tokens': 132}


def event(value):
    return ('data: ' + (value if isinstance(value, str) else json.dumps(value)) + '\n\n').encode()


def blocks(*, incomplete=False, eof_done=False):
    values = [event({'choices': [{'delta': {'content': '合成答案'}, 'finish_reason': None}]}),
              event({'choices': [{'delta': {}, 'finish_reason': 'stop'}]}),
              event({'choices': [], 'usage': USAGE})]
    if not incomplete:
        values.append(b'data: [DONE]' if eof_done else event('[DONE]'))
    return values


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, read_error=None, close_error=None,
                 read_gate=None, close_gate=None):
        self.chunks = list(chunks)
        self.read_error, self.close_error = read_error, close_error
        self.read_gate, self.close_gate = read_gate, close_gate
        self.read_started, self.close_started = asyncio.Event(), asyncio.Event()
        self.closed, self.close_calls, self.iterator_finalized = False, 0, False

    async def __aiter__(self):
        self.read_started.set()
        try:
            if self.read_gate is not None:
                await self.read_gate.wait()
            for chunk in self.chunks:
                yield chunk
            if self.read_error is not None:
                raise self.read_error
        finally:
            self.iterator_finalized = True

    async def aclose(self):
        self.close_calls += 1
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


@asynccontextmanager
async def opened_run(stream, *, status=200, body_size=64, **updates):
    wrappers, requests, responses = [], [], []
    class CapturedStream(ReleasingResponseStream):
        def __init__(self, inner):
            super().__init__(inner)
            wrappers.append(self)
    async def handler(request):
        requests.append(weakref.ref(request))
        response = httpx.Response(status, stream=stream, headers={'content-type': 'text/event-stream'})
        responses.append(weakref.ref(response))
        return response
    class Corpus:
        def request_bytes(self, request_id, group=0):
            return json.dumps({'input': 'UNIQUE_SYNTHETIC_BODY_' + 'x' * body_size,
                               'request_id': request_id}).encode()
    run = TpmRun(config(**updates), transport=httpx.MockTransport(handler))
    run.corpus = Corpus()
    await run._open()
    try:
        with patch('tpm_engine.ReleasingResponseStream', CapturedStream):
            yield run, wrappers, requests, responses
    finally:
        await run._close()


class TpmStreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_passthrough_chunks_close_once_and_release_inner_without_collection(self):
        stream = ControlledStream([b'one', b'two'])
        reference = weakref.ref(stream)
        wrapper = ReleasingResponseStream(stream)
        self.assertEqual([value async for value in wrapper], [b'one', b'two'])
        await wrapper.aclose()
        self.assertTrue(stream.closed)
        self.assertEqual(stream.close_calls, 1)
        self.assertIsNone(wrapper._inner)
        await wrapper.aclose()
        self.assertEqual(stream.close_calls, 1)
        del stream
        self.assertIsNone(reference(), 'simple owned stream should be released by reference counting')
        with self.assertRaises(httpx.StreamClosed):
            await anext(wrapper.__aiter__())

    async def test_close_failure_retains_inner_until_a_later_successful_close(self):
        error = OSError('synthetic-close')
        stream = ControlledStream(close_error=error)
        reference = weakref.ref(stream)
        wrapper = ReleasingResponseStream(stream)
        with self.assertRaises(OSError) as caught:
            await wrapper.aclose()
        self.assertIs(caught.exception, error)
        self.assertIs(wrapper._inner, stream)
        self.assertFalse(stream.closed)
        stream.close_error = None
        await wrapper.aclose()
        self.assertIsNone(wrapper._inner)
        # The deliberately retained exception traceback owns its failing frame;
        # only the wrapper's reference-release guarantee is asserted here.
        self.assertIsNotNone(reference())

    async def test_httpx_metadata_elapsed_request_and_usage_are_preserved(self):
        stream = ControlledStream(blocks())
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream,
                                              headers={'x-synthetic': 'kept'}, extensions={'synthetic': 7}))) as client:
            async with client.stream('POST', 'https://mock.invalid/v1', content=b'synthetic-body') as response:
                wrapper = ReleasingResponseStream(response.stream)
                response.stream = wrapper
                record = {'usage': normalize_usage(None), 'ttft_seconds': None, 'first_token_at': None}
                await consume_sse(response, record, time.monotonic(), 'openai')
            self.assertTrue(response.is_closed)
            self.assertIsNone(wrapper._inner)
            self.assertGreaterEqual(response.elapsed.total_seconds(), 0)
            self.assertEqual(response.headers['x-synthetic'], 'kept')
            self.assertEqual(response.extensions['synthetic'], 7)
            self.assertEqual(response.request.content, b'synthetic-body')
            self.assertEqual(record['usage']['raw'], USAGE)
            self.assertTrue(record['stream_done'])
            self.assertEqual(record['finish_reason'], 'stop')

    async def test_successful_request_closes_before_success_and_preserves_known_fee(self):
        stream = ControlledStream(blocks())
        async with opened_run(stream) as (run, wrappers, _, _):
            row = await run._request('calibration')
            self.assertTrue(row['success'])
            self.assertTrue(stream.closed)
            self.assertIsNone(wrappers[0]._inner)
            self.assertTrue(row['usage']['billing_complete'])
            self.assertGreater(run.ledger.snapshot()['known_cost_usd'], 0)
            self.assertEqual(run.ledger.snapshot()['reserved_cost_usd'], 0)

    async def test_http_errors_close_without_reading_and_keep_unknown_bill_reserve(self):
        for status, category in ((429, 'http_429'), (503, 'http_5xx')):
            with self.subTest(status=status):
                stream = ControlledStream(blocks())
                async with opened_run(stream, status=status) as (run, wrappers, _, _):
                    row = await run._request('calibration')
                    self.assertFalse(row['success'])
                    self.assertEqual(row['error'], category)
                    self.assertFalse(stream.read_started.is_set())
                    self.assertTrue(stream.closed)
                    self.assertIsNone(wrappers[0]._inner)
                    self.assertGreater(run.ledger.snapshot()['reserved_cost_usd'], 0)

    async def test_read_error_and_incomplete_sse_close_but_do_not_become_success(self):
        for stream, category in ((ControlledStream(blocks(incomplete=True)), 'incomplete_stream'),
                                 (ControlledStream(blocks(incomplete=True),
                                                   read_error=httpx.ReadError('synthetic')), 'network_error')):
            with self.subTest(category=category):
                async with opened_run(stream) as (run, wrappers, _, _):
                    row = await run._request('calibration')
                    self.assertFalse(row['success'])
                    self.assertEqual(row['error'], category)
                    self.assertTrue(stream.closed)
                    self.assertIsNone(wrappers[0]._inner)
                    fees = run.ledger.snapshot()
                    self.assertGreater(fees['known_cost_usd'] + fees['reserved_cost_usd'], 0)

    async def test_timeout_while_reading_closes_and_keeps_unknown_reserve(self):
        stream = ControlledStream(blocks(), read_gate=asyncio.Event())
        async with opened_run(stream, timeout_seconds=.05) as (run, wrappers, _, _):
            row = await run._request('calibration')
            self.assertEqual(row['error'], 'timeout')
            self.assertFalse(row['success'])
            self.assertTrue(stream.closed)
            self.assertIsNone(wrappers[0]._inner)
            self.assertGreater(run.ledger.snapshot()['reserved_cost_usd'], 0)

    async def test_cancellation_while_reading_closes_and_preserves_failure_record(self):
        stream = ControlledStream(blocks(), read_gate=asyncio.Event())
        async with opened_run(stream) as (run, wrappers, _, _):
            task = asyncio.create_task(run._request('calibration'))
            await stream.read_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stream.closed)
            self.assertTrue(stream.iterator_finalized)
            self.assertIsNone(wrappers[0]._inner)
            row = run.requests[-1]
            self.assertEqual(row['error'], 'cancelled_unknown_bill')
            self.assertFalse(row['success'])
            self.assertGreater(run.ledger.snapshot()['reserved_cost_usd'], 0)

    async def test_auto_eof_close_failure_keeps_inner_despite_httpx_closed_flag(self):
        stream = ControlledStream(blocks(eof_done=True), close_error=httpx.ReadError('synthetic-close'))
        async with opened_run(stream) as (run, wrappers, _, responses):
            row = await run._request('calibration')
            response = responses[0]()
            self.assertIsNotNone(response)
            self.assertTrue(response.is_closed, 'HTTPX sets this before awaiting transport close')
            self.assertIsNotNone(wrappers[0]._inner)
            self.assertEqual(stream.close_calls, 1, 'closed flag prevents context from retrying transport close')
            self.assertFalse(stream.closed)
            self.assertFalse(row['success'])
            self.assertEqual(row['error'], 'network_error')
            fees = run.ledger.snapshot()
            self.assertGreater(fees['known_cost_usd'] + fees['reserved_cost_usd'], 0)

    async def test_context_close_failure_after_done_keeps_known_usage_and_marks_failure(self):
        stream = ControlledStream(blocks(), close_error=OSError('synthetic-close'))
        async with opened_run(stream) as (run, wrappers, _, responses):
            row = await run._request('calibration')
            self.assertFalse(row['success'])
            self.assertEqual(row['error'], 'client_error')
            self.assertTrue(row['stream_done'])
            self.assertEqual(row['usage']['raw'], USAGE)
            self.assertGreater(run.ledger.snapshot()['known_cost_usd'], 0)
            self.assertIsNotNone(wrappers[0]._inner)
            self.assertTrue(responses[0]().is_closed)

    async def test_timeout_or_cancellation_during_close_does_not_release_inner_or_pass(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                stream = ControlledStream(blocks(), close_gate=asyncio.Event())
                async with opened_run(stream, timeout_seconds=.05 if not cancel else .4) as (run, wrappers, _, _):
                    task = asyncio.create_task(run._request('calibration'))
                    await stream.close_started.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        row = run.requests[-1]
                    else:
                        row = await task
                    self.assertFalse(row['success'])
                    self.assertEqual(row['error'], 'cancelled_unknown_bill' if cancel else 'timeout')
                    self.assertFalse(stream.closed)
                    self.assertIsNotNone(wrappers[0]._inner)
                    self.assertGreater(run.ledger.snapshot()['known_cost_usd'], 0)

    async def test_finished_request_task_retains_summary_not_large_body(self):
        configuration = gc.isenabled(), gc.get_threshold()
        stream = ControlledStream(blocks())
        async with opened_run(stream, body_size=305000) as (run, wrappers, _, _):
            task = asyncio.create_task(run._request('calibration'))
            row = await task
            self.assertTrue(row['success'])
            self.assertGreater(row['body_bytes'], 300000)
            self.assertIsNone(task.get_coro().cr_frame)
            self.assertIsNone(wrappers[0]._inner)
            self.assertNotIn('UNIQUE_SYNTHETIC_BODY_', json.dumps(task.result()))
            self.assertLess(len(json.dumps(task.result())), 10000)
            # HTTPX/async-generator ownership outside this finished Task is not
            # a promise that every HTTP object disappears immediately. The Task
            # owns its small summary, no live coroutine frame or body result.
        self.assertEqual((gc.isenabled(), gc.get_threshold()), configuration)


if __name__ == '__main__':
    unittest.main()
