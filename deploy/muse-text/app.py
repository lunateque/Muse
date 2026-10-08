import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from sentence_transformers import SentenceTransformer

MODEL = "google/embeddinggemma-2"
HOST = "127.0.0.1"
PORT = 8911
THREADS = 2
MAX_BODY = 4096

torch.set_num_threads(THREADS)
model = SentenceTransformer(
    MODEL,
    device="cpu",
    config_kwargs={"vision_config": None, "audio_config": None},
    model_kwargs={"torch_dtype": torch.float32},
)


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path != "/embed":
            self.reply(404, {"detail": "not found"})
            return
        size = int(self.headers.get("Content-Length") or 0)
        try:
            text = json.loads(self.rfile.read(min(size, MAX_BODY)))["text"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError(text)
        except ValueError, KeyError, TypeError:
            self.reply(422, {"detail": "text is required"})
            return
        vector = model.encode(
            [text], prompt_name="query", normalize_embeddings=True, convert_to_numpy=True
        )[0]
        self.reply(200, {"vec": [float(value) for value in vector]})

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
