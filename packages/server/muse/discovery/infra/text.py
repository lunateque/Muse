import httpx

from muse.discovery.domain import TextUnavailableError

TEXT_TIMEOUT_S = 3.0


class HttpTextEncoder:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.url = base_url.rstrip("/") + "/embed"

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self.client.post(self.url, json={"text": text}, timeout=TEXT_TIMEOUT_S)
            response.raise_for_status()
            vector = response.json()["vec"]
            return [float(value) for value in vector]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise TextUnavailableError(str(error)) from error
