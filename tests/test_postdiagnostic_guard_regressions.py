"""Post-diagnostic regressions, not fresh acceptance or model predictions.

These fresh synthetic probes specify general boundary invariants after review
of the frozen comparison. They never import its cases, golds or result files.
"""
from __future__ import annotations

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.planner import verify_proposal
from arrmes.protocol import Call, encode
from arrmes.server import ArrmesRuntimeService
from arrmes.service_router import ConfiguredAdapterRouter
from arrmes.services.arr_writes import NativeArrWriteAdapter
from test_native_arr_writes import ArrFixtureState, OWNER, album, arr_fixture


SERIES = "Morrow Viaduct"
FILM = "Lanterns over Bellwater"
ARTIST = "Cirrus Piano Collective"
ALBUM = "Evening Ledger"
TRANSFER = "Bellwater.Square.2031.1080p.ProbeGroup"


@pytest.mark.parametrize("call,query", [
    (Call("music_search", {"artist": ARTIST, "album": "track 6"}),
     f'Fetch only track 6 from the record "{ALBUM}" by "{ARTIST}" through Lidarr.'),
    (Call("movie_search", {"title": "1080p"}),
     f'Search Radarr indexers for only the 1080p version of the film "{FILM}".'),
    (Call("tv_search_missing", {"title": "episode 6"}),
     f'Search Sonarr indexers for episode 6 of the series "{SERIES}".'),
])
def test_model_entity_cannot_hide_an_unrepresentable_selector(call, query):
    assert verify_proposal(call, query) == Call("clarify", {"slot": "scope"})


@pytest.mark.parametrize("call,query", [
    (Call("music_search", {"artist": ARTIST, "album": "track 6"}),
     f'Search Lidarr indexers for the album "track 6" by "{ARTIST}".'),
    (Call("movie_search", {"title": "1080p"}),
     'Search Radarr indexers for the existing movie titled "1080p".'),
    (Call("tv_search_missing", {"title": "Episode 6"}),
     'Search Sonarr indexers for all missing episodes of the series "Episode 6".'),
])
def test_quoted_literal_selector_words_remain_entity_data(call, query):
    assert verify_proposal(call, query) == call


def test_selector_laundering_is_rejected_before_any_native_preflight():
    class ScriptedProposal:
        family = "hermes"

        def infer(self, *_args, **_kwargs):
            # A deliberately incorrect proposal, not an evaluated-model result.
            return {"raw": encode(Call("music_search", {"artist": ARTIST, "album": "track 6"}), "hermes")}

    state = ArrFixtureState()
    state.artists[0]["artistName"] = ARTIST
    state.albums = [album(8, title="track 6"), album(13, title=ALBUM)]
    with arr_fixture(state) as (native, state):
        runtime = ArrmesRuntimeService(model=ScriptedProposal(),
            executor=BoundedExecutor(ConfiguredAdapterRouter(native)))
        result = runtime.execute(
            f'Fetch only track 6 from the record "{ALBUM}" by "{ARTIST}" through Lidarr.', OWNER)
        assert result["terminal_state"] == "clarify"
        assert not state.calls


@pytest.mark.parametrize("artist_wide", [False, True])
def test_music_confirmation_summary_names_the_actual_album_targets(artist_wide):
    state = ArrFixtureState()
    state.artists[0]["artistName"] = ARTIST
    second = "Bellwater Almanac"
    state.albums = [album(8, title=ALBUM), album(13, title=second)]
    args = {"artist": ARTIST}
    if not artist_wide:
        args["album"] = ALBUM
    with arr_fixture(state) as (native, state):
        preview = NativeArrWriteAdapter(native).execute(Call("music_search", args), OWNER)
        assert preview.status == "confirmation_required"
        assert ARTIST in preview.data["summary"]
        assert ALBUM in preview.data["summary"]
        if artist_wide:
            assert second in preview.data["summary"]
        assert not any(call["method"] == "POST" for call in state.calls)


@pytest.mark.parametrize("call,query", [
    (Call("clarify", {"slot": "scope"}),
     f'For "{SERIES}", I have not chosen between an inventory report and an acquisition task.'),
    (Call("clarify", {"slot": "media_type"}),
     f'Please request "{FILM}"; I have not chosen between its film and television meanings.'),
    (Call("clarify", {"slot": "reference"}),
     'Use the second item from a selection list that I have not supplied.'),
    (Call("unsupported", {"reason": "unsafe"}),
     f'Delete the series "{SERIES}" together with its episode files.'),
    (Call("unsupported", {"reason": "complex"}),
     f'Inspect "{SERIES}" in Sonarr and separately pause the torrent "{TRANSFER}".'),
    (Call("unsupported", {"reason": "out_of_scope"}),
     'Invent a limerick about an imaginary station clock.'),
])
def test_validated_model_controls_remain_stable_across_repeated_boundaries(call, query):
    first = verify_proposal(call, query)
    assert first == call
    assert verify_proposal(first, query) == call


@pytest.mark.parametrize("call,query,expected", [
    (Call("clarify", {"slot": "title"}),
     'Restart Sonarr.', Call("unsupported", {"reason": "unsafe"})),
    (Call("unsupported", {"reason": "out_of_scope"}),
     f'Check Sonarr status for "{SERIES}" and then pause "{TRANSFER}".',
     Call("unsupported", {"reason": "complex"})),
])
def test_hard_safety_and_complex_guards_still_precede_control_preservation(call, query, expected):
    assert verify_proposal(call, query) == expected


@pytest.mark.parametrize("query", [
    f'Search Sonarr indexers for every missing episode of "{SERIES}".',
    f'Search Sonarr indexers for each missing episode of "{SERIES}".',
    f'Search Sonarr indexers for all missing episodes of "{SERIES}".',
])
def test_universal_missing_episode_wording_does_not_invent_a_narrow_selector(query):
    call = Call("tv_search_missing", {"title": SERIES})
    assert verify_proposal(call, query) == call


@pytest.mark.parametrize("restriction", [
    "episode 6", "episodes three through five", "every second missing episode",
    "the newest missing episode", "every missing episode except episode 6",
    "every missing episode from 2 through 5", "each missing episode numbered 6",
    "every missing episode 6", "missing episodes from 2 to 5",
])
def test_universal_wording_never_erases_actual_episode_restrictions(restriction):
    result = verify_proposal(Call("tv_search_missing", {"title": SERIES}),
        f'Search Sonarr indexers for {restriction} of "{SERIES}".')
    assert result.name in {"clarify", "unsupported"}


@pytest.mark.parametrize("restriction", ["after the pilot", "before the finale"])
def test_relative_tail_after_a_universal_target_does_not_widen_to_the_series(restriction):
    result = verify_proposal(Call("tv_search_missing", {"title": SERIES}),
        f'Search Sonarr indexers for every missing episode of "{SERIES}" {restriction}.')
    assert result.name in {"clarify", "unsupported"}


@pytest.mark.parametrize("query", [
    f'Open a new request as me for the movie "{FILM}" from 2009.',
    f'Submit "{FILM}" (2009) as my movie request.',
    f'For the film "{FILM}" from 2009, submit this as a request under my account.',
])
def test_request_self_attribution_is_not_foreign_identity_scope(query):
    call = Call("request_media", {"title": FILM, "media_type": "movie", "year": 2009})
    assert verify_proposal(call, query) == call


def test_request_status_can_describe_its_prior_submission_as_a_request():
    call = Call("request_status", {"title": FILM})
    assert verify_proposal(call,
        f'I filed "{FILM}" as a request. Report the state of my submission.') == call


def test_request_media_class_phrase_is_not_an_account_identity():
    call = Call("request_media", {"title": SERIES, "media_type": "tv"})
    assert verify_proposal(call, f'Submit "{SERIES}" as a TV-series request.') == call


@pytest.mark.parametrize("identity", ["as another user", "as administrator", "as user 17",
                                      "as an unknown account", "on behalf of a different viewer",
                                      'as "Dana"', 'as "user 17"'])
def test_foreign_or_unknown_request_identity_still_clarifies(identity):
    call = Call("request_media", {"title": FILM, "media_type": "movie"})
    assert verify_proposal(call, f'Request the movie "{FILM}" {identity}.').name == "clarify"


@pytest.mark.parametrize("identity", ['as "Dana"', 'as "user 17"'])
def test_quoted_foreign_identity_cannot_redirect_a_caller_owned_status_read(identity):
    call = Call("request_status", {"title": FILM})
    assert verify_proposal(call, f'Check my request status for "{FILM}" {identity}.').name == "clarify"


def test_identity_words_inside_a_quoted_media_title_remain_data():
    call = Call("request_media", {"title": "Notes as Administrator", "media_type": "movie"})
    assert verify_proposal(call, 'Request the movie "Notes as Administrator" as me.') == call


@pytest.mark.parametrize("performer", [
    f'artist = "{ARTIST}"', f'artist="{ARTIST}"',
    f'artist: "{ARTIST}"', f'whose performer is "{ARTIST}"',
    f'by "{ARTIST}"',
])
def test_explicit_performer_relationships_preserve_album_and_artist(performer):
    call = Call("music_search", {"artist": ARTIST, "album": ALBUM})
    assert verify_proposal(call,
        f'Download the album "{ALBUM}", {performer}, using Lidarr indexers.') == call


def test_a_token_inside_one_album_title_cannot_supply_the_artist_role():
    call = Call("music_search", {"artist": "Cirrus", "album": "Ledger"})
    assert verify_proposal(call, 'Download album "Cirrus Ledger" using Lidarr.').name == "clarify"


def test_negated_request_does_not_promote_movie_inventory_to_request_status():
    call = Call("movie_status", {"title": FILM, "year": 2009})
    query = f'Do not request the movie "{FILM}" (2009); inspect its existing library record only.'
    first = verify_proposal(call, query)
    assert first == call
    assert verify_proposal(first, query) == call


@pytest.mark.parametrize("query", [
    f'Restart transfer "{TRANSFER}" by resuming it in qBittorrent.',
    f'Let torrent "{TRANSFER}" transfer again by resuming it now.',
])
def test_explicit_resume_inflection_is_not_a_transfer_inventory_read(query):
    call = Call("torrent_resume", {"title": TRANSFER})
    assert verify_proposal(call, query) == call


def test_resume_status_question_is_not_authorization_to_resume():
    result = verify_proposal(Call("torrent_resume", {"title": TRANSFER}),
        f'Has torrent "{TRANSFER}" resumed yet?')
    assert result.name != "torrent_resume"


@pytest.mark.parametrize("prohibition", [
    "No resuming", "Stop resuming", "Do not keep on resuming", "No unpausing",
])
def test_resume_prohibitions_do_not_authorize_a_state_change(prohibition):
    result = verify_proposal(Call("torrent_resume", {"title": TRANSFER}),
        f'{prohibition} the torrent "{TRANSFER}".')
    assert result.name in {"clarify", "torrent_list", "unsupported"}


def test_negated_history_view_does_not_replace_current_activity():
    call = Call("now_playing", {})
    assert verify_proposal(call, 'Use Tautulli current activity, not its watch-history view.') == call
