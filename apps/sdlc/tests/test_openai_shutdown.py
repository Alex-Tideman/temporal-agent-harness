"""Cancellation must finish SDK tasks and close generators in their own context."""

import asyncio
from contextvars import ContextVar

import pytest
from agents import Agent, Model, RunConfig, Runner

from sdlc_builder.integrations.openai import ProfileModel, consume_stream
from sdlc_builder.models import Profile


@pytest.mark.parametrize("harness_wrapper", [False, True])
async def test_cancel_waits_for_model_cleanup(harness_wrapper):
    started, cleaning, release, closed = (asyncio.Event() for _ in range(4))
    owner = ContextVar("test_model_owner", default=None)

    class WaitingModel(Model):
        async def get_response(self, *args, **kwargs):
            raise AssertionError("Expected streaming")

        async def stream_response(self, *args, **kwargs):
            token = owner.set("stream")
            try:
                started.set()
                await asyncio.Event().wait()
                yield  # pragma: no cover
            finally:
                cleaning.set()
                await release.wait()
                owner.reset(token)
                closed.set()

    result = Runner.run_streamed(
        Agent(name="shutdown test", model=WaitingModel()),
        "Wait until cancelled",
        run_config=RunConfig(tracing_disabled=True),
    )
    if harness_wrapper:
        # Published harness 0.4.0 inspects task.exception() after draining the
        # stream; a cancelled task raises CancelledError at that point.
        original = result.stream_events

        async def wrapped():
            async for event in original():
                yield event
            if result.run_loop_task.done():
                error = result.run_loop_task.exception()
                if error is not None:
                    raise error

        result.stream_events = wrapped

    consumer = asyncio.create_task(consume_stream(result))
    try:
        await asyncio.wait_for(started.wait(), 2)
        consumer.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        await asyncio.sleep(0)
        assert not consumer.done(), "Cancellation abandoned the SDK run loop"
        assert result.is_complete  # SDK flag alone does not mean cleanup finished.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 2)
        assert closed.is_set()
        assert result.run_loop_task.done()
        assert owner.get() is None
    finally:
        release.set()
        result.cancel()
        await asyncio.gather(consumer, result.run_loop_task, return_exceptions=True)


async def test_profile_stream_closes_transport_in_own_context(monkeypatch):
    from sdlc_builder.integrations import openai

    owner = ContextVar("test_transport_owner", default=None)
    lifecycle = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            lifecycle.append("client opened")
            return self

        async def __aexit__(self, *args):
            lifecycle.append("client closed")

    class Transport:
        def __init__(self, **kwargs):
            pass

        async def stream_response(self):
            token = owner.set("transport")
            try:
                yield "event"
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                owner.reset(token)
                lifecycle.append("transport closed")

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setattr(openai, "OpenAIResponsesModel", Transport)
    model = ProfileModel(
        Profile(id="test", label="Test", provider="openai", model="fixture"),
        key="fixture-only",
    )
    events = model.stream_response()
    assert await anext(events) == "event"
    await events.aclose()
    assert owner.get() is None
    assert lifecycle == ["client opened", "transport closed", "client closed"]
