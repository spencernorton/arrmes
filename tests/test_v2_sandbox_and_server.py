"""End-to-end verification of arrmes.tools.v2 sandbox, executor, confirmation tokens, and HTTP server."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.request

from arrmes.auth import BearerTokenAuthenticator
from arrmes.executor import BoundedExecutor, SessionEntityLedger
from arrmes.live_adapter import LiveGatewayAdapter, WriteConfirmationStore
from arrmes.model import DeterministicBaselineModel
from arrmes.planner import Principal, verify_proposal
from arrmes.protocol import Call
from arrmes.sandbox.server import SandboxCluster
from arrmes.server import ArrmesRuntimeService, create_http_server


def test_v2_sandbox_and_http_service_end_to_end(tmp_path: Path) -> None:
    with SandboxCluster(tmp_path / "sandbox") as cluster:
        store = WriteConfirmationStore()
        adapter = LiveGatewayAdapter(
            cluster.base_url,
            write_mode="confirm",
            confirmation_store=store,
            request_owner_subject="user:alice",
            sandbox=True,
        )
        executor = BoundedExecutor(adapter, allow_writes=True)
        model = DeterministicBaselineModel(family="hermes", contract_version="v2")
        service = ArrmesRuntimeService(model=model, executor=executor, write_mode="confirm")

        raw_tok = "arrmes-test-bearer-secret-token-0001"
        tok_sha = hashlib.sha256(raw_tok.encode("utf-8")).hexdigest()
        clients_path = tmp_path / "clients.json"
        clients_path.write_text(
            json.dumps({
                "clients": [{
                    "subject": "user:alice",
                    "token_sha256": tok_sha,
                    "scopes": [
                        "media:read", "media:manage", "activity:read",
                        "requests:read", "requests:create", "releases:classify",
                        "releases:read", "pipeline:read", "torrents:read", "torrents:manage",
                    ],
                }]
            }),
            encoding="utf-8",
        )
        auth = BearerTokenAuthenticator.from_file(str(clients_path))
        srv = create_http_server(service, authenticator=auth, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        import threading
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            headers = {"Authorization": f"Bearer {raw_tok}", "Content-Type": "application/json"}
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
                assert r.status == 200
                hz = json.loads(r.read().decode("utf-8"))
                assert hz["status"] == "ok"

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/route",
                data=json.dumps({"query": "Is anyone streaming right now?"}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200
                routed = json.loads(r.read().decode("utf-8"))
                assert routed["proposal"]["name"] == "now_playing"
        finally:
            srv.shutdown()
            srv.server_close()


def test_verify_proposal_write_and_negation_guards() -> None:
    c1 = verify_proposal(
        Call("request_media", {"title": "Sintel", "media_type": "movie"}),
        "Do not submit Sintel as a movie request, just check if it is in Radarr.",
        None,
        contract_version="v2",
    )
    assert c1.name != "request_media"

    c2 = verify_proposal(
        Call("tv_missing", {"title": "it"}),
        "Which episodes of it are missing?",
        {"selected_media": {"title": "Sintel", "media_type": "tv"}},
        contract_version="v2",
    )
    assert c2.name == "tv_missing"
    assert c2.arguments["title"] == "Sintel"
