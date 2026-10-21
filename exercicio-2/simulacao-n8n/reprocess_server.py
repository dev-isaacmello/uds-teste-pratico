"""/reprocess falso: aceita a mensagem (202) e guarda o que recebeu para a conferência."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

recebidas: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        corpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        recebidas.append({"idempotency_key": self.headers.get("Idempotency-Key"), "corpo": json.loads(corpo or b"{}")})
        self._responder(202, {"aceita": True})

    def do_GET(self) -> None:
        self._responder(200, recebidas)

    def do_DELETE(self) -> None:
        recebidas.clear()
        self._responder(200, [])

    def _responder(self, status: int, doc: object) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(doc).encode())

    def log_message(self, *_: object) -> None:
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
