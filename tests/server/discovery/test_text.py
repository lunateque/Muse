import httpx
import pytest

from muse.discovery.domain import TextUnavailableError
from muse.discovery.infra.text import HttpTextEncoder


def encoder(handler: httpx.MockTransport) -> HttpTextEncoder:
    return HttpTextEncoder(httpx.AsyncClient(transport=handler), "http://text.local:8911/")


async def test_the_text_service_gets_the_text_and_returns_its_vector() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"vec": [0.5, 1, -0.25]})

    vector = await encoder(httpx.MockTransport(answer)).embed("тихая ночь")

    assert vector == [0.5, 1.0, -0.25]
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", "http://text.local:8911/embed")
    assert request.read() == '{"text":"тихая ночь"}'.encode()
    assert request.extensions["timeout"]["read"] == 3.0


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"nope": []}),
        httpx.Response(200, json={"vec": "text"}),
        httpx.Response(200, json={"vec": [1, None]}),
        httpx.Response(200, json=[1, 2]),
    ],
)
async def test_a_failing_or_odd_answer_means_the_text_service_is_unavailable(
    answer: httpx.Response,
) -> None:
    with pytest.raises(TextUnavailableError):
        await encoder(httpx.MockTransport(lambda request: answer)).embed("x")


async def test_a_service_that_cannot_be_reached_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TextUnavailableError):
        await encoder(httpx.MockTransport(refuse)).embed("x")
