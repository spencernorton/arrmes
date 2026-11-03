"""Unit tests for ArrmesNeuralRouterV3, protocol v3 schemas, and backend wiring."""
from __future__ import annotations

from pathlib import Path
import pytest

from arrmes.backends import create_model
from arrmes.catalog import contract_hash_v2, contract_hash_v3, tools_v2, tools_v3
from arrmes.neural_router import ArrmesNeuralRouterV3
from arrmes.protocol import decode, normalized

ROUTER_DIR = Path(__file__).resolve().parents[1] / "models" / "arrmes-neural-router-v3"


def test_v2_contract_hash_preserved_and_v3_versioned() -> None:
    assert contract_hash_v2() == "e34f94ec5110423f7e5dfc0309c5e8f10988f158253559f88f34d9d977a2efe8"
    assert len(tools_v2()) == 20
    assert len(tools_v3()) == 23
    assert contract_hash_v3() != contract_hash_v2()


@pytest.mark.skipif(not (ROUTER_DIR / "router_weights.npz").is_file(), reason="router_weights.npz not present")
def test_neural_router_v2_core_and_contrast_queries() -> None:
    model = create_model(str(ROUTER_DIR), family="hermes", backend="neural-router", contract_version="v2")
    assert isinstance(model, ArrmesNeuralRouterV3)

    out1 = model.infer('For a shelf audit, mark whether the television title "Signal in the Reedbed" is already held in Sonarr.')
    c1 = normalized(decode(out1["raw_unverified"], family="hermes"))
    assert c1 == {"name": "tv_status", "arguments": {"title": "Signal in the Reedbed"}}

    out2 = model.infer('Start the search for all missing "Signal in the Reedbed" episodes rather than reporting status.')
    c2 = normalized(decode(out2["raw_unverified"], family="hermes"))
    assert c2 == {"name": "tv_search_missing", "arguments": {"title": "Signal in the Reedbed"}}

    out3 = model.infer('Resume the existing torrent "Opaline.Borough.1989.1080p.FableGroup"; do not create a new download.')
    c3 = normalized(decode(out3["raw_unverified"], family="hermes"))
    assert c3 == {"name": "torrent_resume", "arguments": {"title": "Opaline.Borough.1989.1080p.FableGroup"}}

    out4 = model.infer('The listening card names "Mireille Vane" as artist and "Paper Astrolabe" as album; check that album in Lidarr.')
    c4 = normalized(decode(out4["raw_unverified"], family="hermes"))
    assert c4 == {"name": "music_status", "arguments": {"artist": "Mireille Vane", "album": "Paper Astrolabe"}}


@pytest.mark.skipif(not (ROUTER_DIR / "router_weights.npz").is_file(), reason="router_weights.npz not present")
def test_neural_router_v3_native_service_and_bazarr_queries() -> None:
    model = create_model(str(ROUTER_DIR), family="hermes", backend="neural-router", contract_version="v3")

    out_sq = model.infer("Check Sonarr health")
    c_sq = normalized(decode(out_sq["raw_unverified"], family="hermes", allow_v3=True))
    assert c_sq == {"name": "service_query", "arguments": {"service": "sonarr", "operation": "health"}}

    out_bz_s = model.infer('Find English non-forced non-HI subtitles for the movie "Solaris" (1972) in Bazarr')
    c_bz_s = normalized(decode(out_bz_s["raw_unverified"], family="hermes", allow_v3=True))
    assert c_bz_s == {
        "name": "bazarr_subtitle_search",
        "arguments": {
            "media_type": "movie",
            "title": "Solaris",
            "year": 1972,
            "language": "en",
            "forced": False,
            "hearing_impaired": False,
        },
    }

    cand_id = "0123456789abcdef01234567"
    out_bz_d = model.infer(
        f'Download English non-forced non-HI subtitles candidate {cand_id} for the movie "Solaris" (1972) in Bazarr'
    )
    c_bz_d = normalized(decode(out_bz_d["raw_unverified"], family="hermes", allow_v3=True))
    assert c_bz_d == {
        "name": "bazarr_subtitle_download",
        "arguments": {
            "media_type": "movie",
            "title": "Solaris",
            "year": 1972,
            "language": "en",
            "forced": False,
            "hearing_impaired": False,
            "candidate_id": cand_id,
        },
    }


def _write_synthetic_npz(npz_path: Path) -> None:
    import array
    import struct
    import zipfile
    from arrmes.neural_router import CLASS_TO_IDX, ROUTER_CLASSES, TOTAL_FEATURES, featurize_query

    h_dim = 8
    c_dim = len(ROUTER_CLASSES)
    w1 = array.array("f", [0.0] * (TOTAL_FEATURES * h_dim))
    b1 = array.array("f", [0.0] * h_dim)
    w2 = array.array("f", [0.0] * (h_dim * c_dim))
    b2 = array.array("f", [0.0] * c_dim)

    # Wire structural indicator features directly through hidden units to test classes
    q_tv = 'For a shelf audit, mark whether the television title "Signal in the Reedbed" is already held in Sonarr.'
    x_tv = featurize_query(q_tv)
    for i, val in enumerate(x_tv):
        if float(val) != 0.0:
            w1[i * h_dim + 0] = float(val)
    w2[0 * c_dim + CLASS_TO_IDX["tv_status"]] = 10.0

    def _npy_bytes(shape: tuple[int, ...], arr: array.array) -> bytes:
        shape_str = f"({shape[0]},)" if len(shape) == 1 else f"({shape[0]}, {shape[1]})"
        hdr = f"{{'descr': '<f4', 'fortran_order': False, 'shape': {shape_str}, }}"
        pad = 64 - ((10 + len(hdr) + 1) % 64)
        hdr_bytes = (hdr + (" " * pad) + "\n").encode("latin1")
        return b"\x93NUMPY\x01\x00" + struct.pack("<H", len(hdr_bytes)) + hdr_bytes + arr.tobytes()

    with zipfile.ZipFile(npz_path, "w") as zf:
        zf.writestr("W1.npy", _npy_bytes((TOTAL_FEATURES, h_dim), w1))
        zf.writestr("b1.npy", _npy_bytes((h_dim,), b1))
        zf.writestr("W2.npy", _npy_bytes((h_dim, c_dim), w2))
        zf.writestr("b2.npy", _npy_bytes((c_dim,), b2))


def test_neural_router_synthetic_npz_and_stdlib_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arrmes.neural_router as nr_mod

    npz_path = tmp_path / "router_weights.npz"
    _write_synthetic_npz(npz_path)

    # Test with default backend
    m1 = create_model(str(tmp_path), family="hermes", backend="neural-router", contract_version="v3")
    out1 = m1.infer('For a shelf audit, mark whether the television title "Signal in the Reedbed" is already held in Sonarr.')
    assert normalized(decode(out1["raw"], family="hermes")) == {
        "name": "tv_status",
        "arguments": {"title": "Signal in the Reedbed"},
    }
    out_sq = m1.infer("Check Sonarr health")
    assert normalized(decode(out_sq["raw"], family="hermes", allow_v3=True)) == {
        "name": "service_query",
        "arguments": {"service": "sonarr", "operation": "health"},
    }

    # Test pure-stdlib fallback (np = None)
    monkeypatch.setattr(nr_mod, "np", None)
    m2 = ArrmesNeuralRouterV3(str(tmp_path), family="hermes", contract_version="v3")
    out2 = m2.infer('For a shelf audit, mark whether the television title "Signal in the Reedbed" is already held in Sonarr.')
    assert normalized(decode(out2["raw"], family="hermes")) == {
        "name": "tv_status",
        "arguments": {"title": "Signal in the Reedbed"},
    }


def test_unicode_and_region_entity_extraction_and_missing_search_routing() -> None:
    from arrmes.neural_router import _extract_unquoted_entity
    from arrmes.services import route_service_query

    assert _extract_unquoted_entity("How many Café Odyssey episodes are missing?") == "Café Odyssey"
    assert _extract_unquoted_entity("Search Sonarr for missing Café Odyssey episodes") == "Café Odyssey"
    assert _extract_unquoted_entity("Search all missing Café Odyssey regular episodes") == "Café Odyssey"
    assert _extract_unquoted_entity("Which episodes of Island Horizon (US) are missing in Sonarr?") == "Island Horizon (US)"
    assert route_service_query("Search Sonarr for missing Café Odyssey episodes in my library") is None
    assert route_service_query("Trigger a Sonarr search for missing Café Odyssey episodes") is None
    assert route_service_query("How many Café Odyssey episodes are missing in Sonarr?") is None
