"""Test stream init."""

import asyncio
import io
import logging
import threading
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import av
import pytest

from homeassistant.components import stream as stream_module
from homeassistant.components.stream import (
    SOURCE_TIMEOUT,
    StreamClientError,
    StreamOpenClientError,
    StreamWorkerError,
    __name__ as stream_name,
    async_check_stream_client_error,
    create_stream,
)
from homeassistant.components.stream.const import ATTR_PREFER_TCP, DOMAIN, HLS_PROVIDER
from homeassistant.const import CONF_VERIFY_SSL, EVENT_LOGGING_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .common import dynamic_stream_settings

TIMEOUT = 15
STREAM_SOURCE = "rtsp://foobar"


async def test_stream_not_setup(hass: HomeAssistant, h264_video) -> None:
    """Test hls stream.

    Purposefully not mocking anything here to test full
    integration with the stream component.
    """
    with pytest.raises(HomeAssistantError, match="Stream integration is not set up"):
        create_stream(hass, "rtsp://foobar", {}, dynamic_stream_settings())

    with pytest.raises(HomeAssistantError, match="Stream integration is not set up"):
        await async_check_stream_client_error(hass, "rtsp://foobar")


async def test_log_levels(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that the worker logs the url without username and password."""

    await async_setup_component(hass, DOMAIN, {"stream": {}})

    # These namespaces should only pass log messages when the stream logger
    # is at logging.DEBUG or below
    namespaces_to_toggle = (
        "mp4",
        "h264",
        "hevc",
        "rtsp",
        "tcp",
        "tls",
        "mpegts",
        "NULL",
    )

    logging.getLogger(stream_name).setLevel(logging.INFO)
    hass.bus.async_fire(EVENT_LOGGING_CHANGED)
    await hass.async_block_till_done()

    # Since logging is at INFO, these should not pass
    for namespace in namespaces_to_toggle:
        av.logging.log(av.logging.ERROR, namespace, "SHOULD NOT PASS")

    logging.getLogger(stream_name).setLevel(logging.DEBUG)
    hass.bus.async_fire(EVENT_LOGGING_CHANGED)
    await hass.async_block_till_done()

    # Since logging is now at DEBUG, these should now pass
    for namespace in namespaces_to_toggle:
        av.logging.log(av.logging.ERROR, namespace, "SHOULD PASS")

    # Even though logging is at DEBUG, these should not pass
    av.logging.log(av.logging.WARNING, "mp4", "SHOULD NOT PASS")
    av.logging.log(av.logging.WARNING, "swscaler", "SHOULD NOT PASS")

    assert "SHOULD PASS" in caplog.text
    assert "SHOULD NOT PASS" not in caplog.text


async def test_check_open_stream_params(hass: HomeAssistant) -> None:
    """Test check open stream params."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})

    container_mock = MagicMock()
    source = "rtsp://foobar"

    with patch("av.open", return_value=container_mock) as open_mock:
        await async_check_stream_client_error(hass, source)

    options = {
        "rtsp_flags": ATTR_PREFER_TCP,
        "stimeout": "5000000",
        "tls_verify": "0",
    }
    open_mock.assert_called_once_with(source, options=options, timeout=SOURCE_TIMEOUT)
    container_mock.close.assert_called_once()

    container_mock.reset_mock()
    with (
        patch("av.open", return_value=container_mock) as open_mock,
        pytest.raises(HomeAssistantError, match="Invalid stream options"),
    ):
        await async_check_stream_client_error(hass, source, {"foo": "bar"})


@pytest.mark.parametrize(
    ("error", "enum_result"),
    [
        pytest.param(
            av.HTTPBadRequestError(400, ""),
            StreamClientError.BadRequest,
            id="BadRequest",
        ),
        pytest.param(
            av.HTTPUnauthorizedError(401, ""),
            StreamClientError.Unauthorized,
            id="Unauthorized",
        ),
        pytest.param(
            av.HTTPForbiddenError(403, ""), StreamClientError.Forbidden, id="Forbidden"
        ),
        pytest.param(
            av.HTTPNotFoundError(404, ""), StreamClientError.NotFound, id="NotFound"
        ),
        pytest.param(
            av.HTTPOtherClientError(408, ""), StreamClientError.Other, id="Other"
        ),
    ],
)
async def test_try_open_stream_error(
    hass: HomeAssistant, error: av.HTTPClientError, enum_result: StreamClientError
) -> None:
    """Test trying to open a stream."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})

    with (
        patch("av.open", side_effect=error),
        pytest.raises(StreamOpenClientError) as ex,
    ):
        await async_check_stream_client_error(hass, "rtsp://foobar")
    assert ex.value.error_code is enum_result


@pytest.mark.parametrize(
    ("options", "expected_pyav_options"),
    [
        (
            {},
            {"rtsp_flags": "prefer_tcp", "stimeout": "5000000", "tls_verify": "0"},
        ),
        (
            {"rtsp_transport": "udp"},
            {
                "rtsp_flags": "prefer_tcp",
                "rtsp_transport": "udp",
                "stimeout": "5000000",
                "tls_verify": "0",
            },
        ),
        (
            {"use_wallclock_as_timestamps": True},
            {
                "rtsp_flags": "prefer_tcp",
                "stimeout": "5000000",
                "tls_verify": "0",
                "use_wallclock_as_timestamps": "1",
            },
        ),
    ],
)
async def test_convert_stream_options(
    hass: HomeAssistant,
    options: dict[str, Any],
    expected_pyav_options: dict[str, Any],
) -> None:
    """Test stream options."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})

    container_mock = MagicMock()
    source = "rtsp://foobar"

    with patch("av.open", return_value=container_mock) as open_mock:
        await async_check_stream_client_error(hass, source, options)

    open_mock.assert_called_once_with(
        source, options=expected_pyav_options, timeout=SOURCE_TIMEOUT
    )
    container_mock.close.assert_called_once()


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("rtsps://foobar", id="rtsps"),
        pytest.param("https://foobar", id="https"),
        # an http source can redirect to https
        pytest.param("http://foobar", id="http"),
    ],
)
async def test_tls_verify_disabled(hass: HomeAssistant, source: str) -> None:
    """Test TLS certificate verification is disabled for non-RTSP sources."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})

    with patch("av.open") as open_mock:
        await async_check_stream_client_error(hass, source)

    open_mock.assert_called_once_with(
        source, options={"tls_verify": "0"}, timeout=SOURCE_TIMEOUT
    )


@pytest.mark.parametrize(
    ("verify_ssl", "tls_verify"),
    [
        pytest.param(True, "1", id="enabled"),
        pytest.param(False, "0", id="disabled"),
    ],
)
async def test_verify_ssl_option(
    hass: HomeAssistant, verify_ssl: bool, tls_verify: str
) -> None:
    """Test the verify_ssl stream option controls TLS certificate verification."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    source = "rtsps://foobar"

    with patch("av.open") as open_mock:
        await async_check_stream_client_error(
            hass, source, {CONF_VERIFY_SSL: verify_ssl}
        )

    open_mock.assert_called_once_with(
        source, options={"tls_verify": tls_verify}, timeout=SOURCE_TIMEOUT
    )


async def test_stop_during_pending_source_update(
    hass: HomeAssistant, h264_video: io.BytesIO
) -> None:
    """Test that stop() is not lost when an update_source() restart is pending."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    worker_open = threading.Event()
    worker_wake = threading.Event()
    opened_sources: list[str] = []
    real_open = av.open

    def blocking_open(stream_source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stream_source, str):
            opened_sources.append(stream_source)
            worker_open.set()
            worker_wake.wait()
            return real_open(io.BytesIO(h264_video.getvalue()))
        return real_open(stream_source, *args, **kwargs)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)

    with patch("av.open", new=blocking_open):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)

            stream.update_source(f"{STREAM_SOURCE}-updated")
            stop_task = hass.async_create_task(stream.stop())
            await asyncio.sleep(0)
            worker_wake.set()
            await stop_task

            assert opened_sources == [STREAM_SOURCE]
        finally:
            worker_wake.set()
            await stream.stop()


async def test_update_source_while_stopping(
    hass: HomeAssistant, h264_video: io.BytesIO
) -> None:
    """Test that update_source() does not undo a stop() in progress."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    worker_open = threading.Event()
    worker_wake = threading.Event()
    opened_sources: list[str] = []
    real_open = av.open

    def blocking_open(stream_source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stream_source, str):
            opened_sources.append(stream_source)
            worker_open.set()
            worker_wake.wait()
            return real_open(io.BytesIO(h264_video.getvalue()))
        return real_open(stream_source, *args, **kwargs)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)

    with patch("av.open", new=blocking_open):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)

            stop_task = hass.async_create_task(stream.stop())
            await asyncio.sleep(0)
            stream.update_source(f"{STREAM_SOURCE}-updated")
            worker_wake.set()
            await stop_task

            assert opened_sources == [STREAM_SOURCE]
        finally:
            worker_wake.set()
            await stream.stop()


async def test_update_source_during_retry_backoff(
    hass: HomeAssistant, h264_video: io.BytesIO, should_retry: Mock
) -> None:
    """Test that update_source() wakes the worker during retry backoff."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = True
    worker_open = threading.Event()
    worker_wake = threading.Event()
    in_backoff = asyncio.Event()
    opened_sources: list[str] = []
    real_open = av.open

    def blocking_open(stream_source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stream_source, str):
            opened_sources.append(stream_source)
            if len(opened_sources) == 1:
                raise av.FFmpegError(1, "Simulated open failure")
            worker_open.set()
            worker_wake.wait()
            return real_open(io.BytesIO(h264_video.getvalue()))
        return real_open(stream_source, *args, **kwargs)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)
    stream.set_update_callback(lambda: None if stream.available else in_backoff.set())

    with (
        patch("av.open", new=blocking_open),
        patch("homeassistant.components.stream.STREAM_RESTART_INCREMENT", 3600),
    ):
        try:
            await stream.start()
            await asyncio.wait_for(in_backoff.wait(), TIMEOUT)
            assert not stream.available

            updated_source = f"{STREAM_SOURCE}-updated"
            stream.update_source(updated_source)
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)
            assert opened_sources == [STREAM_SOURCE, updated_source]
        finally:
            worker_wake.set()
            await stream.stop()


async def test_stream_source_failure_latched_through_retry(
    hass: HomeAssistant, should_retry: Mock
) -> None:
    """Keep failed-source status through the retry's transient available=True."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = True
    retry_started = threading.Event()
    new_source_started = threading.Event()
    attempts: list[str] = []
    source = STREAM_SOURCE
    fresh_source = f"{STREAM_SOURCE}-fresh"

    def stream_worker(stream_source: str, *args: Any) -> None:
        attempts.append(stream_source)
        if stream_source == source and attempts.count(source) == 1:
            raise StreamWorkerError("Simulated expired stream")
        if stream_source == source:
            retry_started.set()
        else:
            new_source_started.set()
        args[-1].wait(TIMEOUT)

    stream = create_stream(hass, source, {}, dynamic_stream_settings())
    provider = stream.add_provider(HLS_PROVIDER)
    stream.access_token = "test-token"

    with (
        patch(
            "homeassistant.components.stream.worker.stream_worker",
            side_effect=stream_worker,
        ),
        patch("homeassistant.components.stream.STREAM_RESTART_INCREMENT", 0.01),
    ):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(retry_started.wait, TIMEOUT)
            await hass.async_block_till_done()

            assert stream.available
            assert stream.source_attempted
            assert stream.source_failed

            stream.update_source(fresh_source)
            # The worker may already have selected the new source; the
            # atomic handoff is verified by the following test.
            assert not stream.source_failed
            assert await hass.async_add_executor_job(new_source_started.wait, TIMEOUT)

            assert stream.source_attempted
            assert not stream.source_failed
            assert stream.outputs()[HLS_PROVIDER] is provider
            assert stream.access_token == "test-token"
            assert attempts == [source, source, fresh_source]
        finally:
            await asyncio.wait_for(stream.stop(), timeout=TIMEOUT)


async def test_source_update_atomic_with_worker_attempt(
    hass: HomeAssistant, should_retry: Mock
) -> None:
    """Never open a newly allocated source twice across a worker handoff."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = True

    original_begin = stream_module._WorkerControl.begin_attempt  # pylint: disable=protected-access
    initial_selected = threading.Event()
    release_selection = threading.Event()
    fresh_opened = threading.Event()
    attempts: list[str] = []
    new_source = f"{STREAM_SOURCE}-new"

    def pause_after_selection(control: Any) -> tuple[str, int] | None:
        chosen = original_begin(control)
        if chosen is not None and chosen[0] == STREAM_SOURCE:
            initial_selected.set()
            assert release_selection.wait(TIMEOUT)
        return chosen

    def stream_worker(source: str, *args: Any) -> None:
        attempts.append(source)
        if source == new_source:
            fresh_opened.set()
        args[-1].wait(TIMEOUT)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)

    with (
        patch(
            "homeassistant.components.stream._WorkerControl.begin_attempt",
            new=pause_after_selection,
        ),
        patch(
            "homeassistant.components.stream.worker.stream_worker",
            side_effect=stream_worker,
        ),
    ):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(initial_selected.wait, TIMEOUT)
            assert stream.source_attempted
            stream.update_source(new_source)
            # A new URL is not considered consumed until its atomic selection.
            assert not stream.source_attempted
            release_selection.set()
            assert await hass.async_add_executor_job(fresh_opened.wait, TIMEOUT)
            assert stream.source_attempted
            assert not stream.source_failed
            assert attempts == [STREAM_SOURCE, new_source]
        finally:
            release_selection.set()
            await asyncio.wait_for(stream.stop(), timeout=TIMEOUT)

    assert attempts == [STREAM_SOURCE, new_source]


async def test_source_update_during_stop_preserves_new_source_state(
    hass: HomeAssistant, should_retry: Mock
) -> None:
    """A new source arriving while stop joins must stay unattempted."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = False
    old_worker_started = threading.Event()
    release_old_worker = threading.Event()
    fresh_worker_started = threading.Event()
    new_source = f"{STREAM_SOURCE}-fresh"
    opened_sources: list[str] = []

    def stream_worker(source: str, *args: Any) -> None:
        opened_sources.append(source)
        if source == STREAM_SOURCE:
            old_worker_started.set()
            assert release_old_worker.wait(TIMEOUT)
        else:
            fresh_worker_started.set()
            args[-1].wait(TIMEOUT)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)
    with patch(
        "homeassistant.components.stream.worker.stream_worker",
        side_effect=stream_worker,
    ):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(old_worker_started.wait, TIMEOUT)
            stop_task = hass.async_create_task(stream.stop())
            # Ensure the old control is marked stopped but its thread has not
            # joined before the URL is updated.
            for _ in range(100):
                if stream._worker_control.is_stopped:
                    break
                await asyncio.sleep(0)
            assert stream._worker_control.is_stopped
            stream.update_source(new_source)
            release_old_worker.set()
            await asyncio.wait_for(stop_task, TIMEOUT)

            assert stream.source == new_source
            assert not stream.source_attempted
            assert not stream.source_failed
            stream.add_provider(HLS_PROVIDER)
            await stream.start()
            assert await hass.async_add_executor_job(fresh_worker_started.wait, TIMEOUT)
            assert opened_sources == [STREAM_SOURCE, new_source]
        finally:
            release_old_worker.set()
            await asyncio.wait_for(stream.stop(), TIMEOUT)


async def test_stale_worker_completion_does_not_remove_refreshed_provider(
    hass: HomeAssistant, should_retry: Mock
) -> None:
    """Deferred cleanup must not delete HLS output after source replacement."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = False
    old_worker_started = threading.Event()
    new_worker_started = threading.Event()
    completion_scheduled = asyncio.Event()
    pending_completions: list[Any] = []
    source = STREAM_SOURCE
    new_source = f"{STREAM_SOURCE}-next"

    def stream_worker(current: str, *args: Any) -> None:
        if current == source:
            old_worker_started.set()
            return
        new_worker_started.set()
        args[-1].wait(TIMEOUT)

    original_create_task = hass.create_task

    def intercept_worker_cleanup(coro: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            "worker_finished" in getattr(coro, "__qualname__", "")
            and not pending_completions
        ):
            pending_completions.append(coro)
            hass.loop.call_soon_threadsafe(completion_scheduled.set)
            return MagicMock()
        return original_create_task(coro, *args, **kwargs)

    stream = create_stream(hass, source, {}, dynamic_stream_settings())
    provider = stream.add_provider(HLS_PROVIDER)
    stream.access_token = "test-token"

    with (
        patch(
            "homeassistant.components.stream.worker.stream_worker",
            side_effect=stream_worker,
        ),
        patch.object(hass, "create_task", side_effect=intercept_worker_cleanup),
    ):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(old_worker_started.wait, TIMEOUT)
            await asyncio.wait_for(completion_scheduled.wait(), TIMEOUT)
            assert pending_completions

            stream.update_source(new_source)
            # Simulate the old worker's callback running after the refresh.
            for coro in pending_completions:
                await coro
            assert stream.outputs()[HLS_PROVIDER] is provider
            assert stream.access_token == "test-token"

            await stream.start()
            assert await hass.async_add_executor_job(new_worker_started.wait, TIMEOUT)
            assert stream.outputs()[HLS_PROVIDER] is provider
        finally:
            await asyncio.wait_for(stream.stop(), TIMEOUT)


async def test_output_added_while_stopping_is_kept(
    hass: HomeAssistant, h264_video: io.BytesIO, should_retry: Mock
) -> None:
    """Test that an output added while the stream stops survives the restart."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = True
    worker_open = threading.Event()
    worker_wake = threading.Event()
    opened_sources: list[str] = []
    real_open = av.open

    def blocking_open(stream_source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stream_source, str):
            opened_sources.append(stream_source)
            worker_open.set()
            worker_wake.wait()
            return real_open(io.BytesIO(h264_video.getvalue()))
        return real_open(stream_source, *args, **kwargs)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    output = stream.add_provider(HLS_PROVIDER)

    with patch("av.open", new=blocking_open):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)

            # Last viewer leaves: stream stops while its worker is still busy
            stop_task = hass.async_create_task(stream.remove_provider(output))
            await asyncio.sleep(0)

            # New viewer arrives before the old worker exits
            worker_open.clear()
            new_output = stream.add_provider(HLS_PROVIDER)
            start_task = hass.async_create_task(stream.start())
            worker_wake.set()
            await stop_task
            await start_task
            await hass.async_block_till_done()

            assert stream.outputs() == {HLS_PROVIDER: new_output}
            assert stream.endpoint_url(HLS_PROVIDER).startswith("/api/hls/")
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)
            assert opened_sources == [STREAM_SOURCE, STREAM_SOURCE]
        finally:
            worker_wake.set()
            await stream.stop()


async def test_start_after_cancelled_stop_waits_for_worker(
    hass: HomeAssistant, h264_video: io.BytesIO, should_retry: Mock
) -> None:
    """Test that start() waits for a stopping worker after a cancelled stop()."""
    await async_setup_component(hass, DOMAIN, {"stream": {}})
    should_retry.return_value = True
    worker_open = threading.Event()
    first_wake = threading.Event()
    second_wake = threading.Event()
    opened_sources: list[str] = []
    real_open = av.open

    def blocking_open(stream_source: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stream_source, str):
            opened_sources.append(stream_source)
            worker_open.set()
            if len(opened_sources) == 1:
                first_wake.wait()
            else:
                second_wake.wait()
            return real_open(io.BytesIO(h264_video.getvalue()))
        return real_open(stream_source, *args, **kwargs)

    stream = create_stream(hass, STREAM_SOURCE, {}, dynamic_stream_settings())
    stream.add_provider(HLS_PROVIDER)

    with patch("av.open", new=blocking_open):
        try:
            await stream.start()
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)

            stop_task = hass.async_create_task(stream.stop())
            await asyncio.sleep(0)
            stop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop_task

            # start() must not launch a second worker until the first exits
            worker_open.clear()
            stream.add_provider(HLS_PROVIDER)
            start_task = hass.async_create_task(stream.start())
            await asyncio.sleep(0)
            assert not start_task.done()
            assert opened_sources == [STREAM_SOURCE]

            first_wake.set()
            await start_task
            assert await hass.async_add_executor_job(worker_open.wait, TIMEOUT)
            assert opened_sources == [STREAM_SOURCE, STREAM_SOURCE]
        finally:
            first_wake.set()
            second_wake.set()
            await stream.stop()
