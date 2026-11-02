"""Isolated native protocol fixtures, not upstream-binary or neural qualification."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

import pytest

from arrmes.executor import BoundedExecutor
from arrmes.planner import Principal
from arrmes.protocol import Call, InvalidCall, encode, normalized
from arrmes.sandbox import NetworkIsolationGuard
from arrmes.server import ArrmesRuntimeService
from arrmes.services import ServiceConfig, ServiceQueryAdapter
from arrmes.services.arr_writes import NativeArrWriteAdapter

OWNER = Principal("fixture-operator", frozenset({"media:manage"}))
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TV = Call("tv_search_missing", {"title":"Fixture series"})
MOVIE = Call("movie_search", {"title":"Fixture movie","year":2020})
MUSIC = Call("music_search", {"artist":"Fixture artist","album":"Fixture album"})
ARTIST = Call("music_search", {"artist":"Fixture artist"})


def episode(identity, **changes):
    return {"id":identity,"seriesId":1,"seasonNumber":1,"episodeNumber":identity,
            "monitored":True,"hasFile":False,"airDateUtc":"2020-01-01T00:00:00Z",**changes}


def album(identity, **changes):
    return {"id":identity,"artistId":3,"title":"Fixture album","monitored":True,
            "releaseDate":"2020-01-01T00:00:00Z","statistics":{"trackCount":10,"trackFileCount":5},**changes}


@dataclass
class ArrFixtureState:
    versions: dict = field(default_factory=lambda:{"sonarr":"4.0.20.3014","radarr":"6.4.4.10685","lidarr":"3.1.6.5079"})
    starts: dict = field(default_factory=lambda:{s:"2026-01-01T00:00:00Z" for s in ("sonarr","radarr","lidarr")})
    series: list = field(default_factory=lambda:[{"id":1,"title":"Fixture series","year":2020,"monitored":True,"path":"/fixture/private/series"}])
    movies: list = field(default_factory=lambda:[{"id":2,"title":"Fixture movie","year":2020,"monitored":True,"hasFile":False,"path":"/fixture/private/movie"}])
    artists: list = field(default_factory=lambda:[{"id":3,"artistName":"Fixture artist","monitored":True,"path":"/fixture/private/artist"}])
    episodes: list = field(default_factory=lambda:[episode(1),episode(2,airDateUtc="2050-01-01T00:00:00Z"),episode(3,airDateUtc=None),
        episode(4,hasFile=True),episode(5,monitored=False),episode(6,seasonNumber=0),episode(7,seasonNumber=2)])
    albums: list = field(default_factory=lambda:[album(8),album(9,title="Complete album",statistics={"trackCount":10,"trackFileCount":10}),
        album(10,title="Future album",releaseDate="2050-01-01T00:00:00Z"),album(11,title="Undated album",releaseDate=None),
        album(12,title="Unmonitored album",monitored=False),album(13,title="Other missing album",statistics={"trackCount":8,"trackFileCount":0})])
    calls: list = field(default_factory=list)
    commands: dict = field(default_factory=dict)
    faults: dict = field(default_factory=dict)
    post_behavior: str = "queued"
    observation_behavior: str = "same"
    job_status: object = "queued"
    job_result: str = "unknown"


@contextmanager
def arr_fixture(state=None, **options):
    state=state or ArrFixtureState()
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body):
            raw=body if isinstance(body,bytes) else json.dumps(body).encode()
            self.send_response(code);self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        def location(self):
            url=urllib.parse.urlsplit(self.path);parts=url.path.split("/",2)
            return parts[1],"/"+parts[2],urllib.parse.parse_qs(url.query)
        def do_GET(self):
            service,path,query=self.location();state.calls.append({"method":"GET","service":service,"path":path,"query":query})
            if self.headers.get("X-Api-Key")!="fixture-key":return self.respond(401,{})
            if (service,path) in state.faults:return self.respond(*state.faults[(service,path)])
            if path.endswith("/system/status"):
                return self.respond(200,{"appName":service.capitalize(),"version":state.versions[service],"startTime":state.starts[service]})
            if path=="/api/v3/series":return self.respond(200,state.series)
            if path=="/api/v3/movie":return self.respond(200,state.movies)
            if path=="/api/v1/artist":return self.respond(200,state.artists)
            if path=="/api/v3/episode":
                assert query=={"seriesId":["1"]}
                return self.respond(200,state.episodes)
            if path=="/api/v1/album":
                assert query=={"artistId":["3"]}
                return self.respond(200,state.albums)
            if "/command/" in path:
                if state.observation_behavior=="unavailable":return self.respond(503,{})
                resource=deepcopy(state.commands[service]);resource.update(status=state.job_status,result=state.job_result)
                if state.observation_behavior=="wrong_id":resource["id"]+=1
                if state.observation_behavior=="wrong_name":resource["name"]="MissingEpisodeSearch"
                if state.observation_behavior=="wrong_targets":
                    key=next(k for k in resource["body"] if k!="name");resource["body"][key]=[999]
                if state.observation_behavior=="extra_scope":resource["body"]["seriesId"]=999
                return self.respond(200,resource)
            self.respond(404,{})
        def do_POST(self):
            service,path,_=self.location();body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append({"method":"POST","service":service,"path":path,"body":body})
            assert path in {"/api/v3/command","/api/v1/command"}
            if state.post_behavior=="disconnect":self.close_connection=True;return
            if state.post_behavior=="redirect":
                self.send_response(302);self.send_header("Location","/forbidden-redirect");self.end_headers();return
            if state.post_behavior in {"rejected","error"}:return self.respond(400 if state.post_behavior=="rejected" else 500,{})
            if state.post_behavior=="invalid_json":return self.respond(201,b'{"id":17,"id":99}')
            if state.post_behavior=="empty202":return self.respond(202,b'')
            resource={"id":17,"name":body["name"],"body":body,"status":"queued","result":"unknown"}
            if state.post_behavior=="wrong_name":resource["name"]="MissingEpisodeSearch"
            if state.post_behavior=="missing_scope":resource["body"]={"name":body["name"]}
            state.commands[service]=deepcopy(resource)
            self.respond(200 if state.post_behavior=="unexpected200" else 201,resource)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    try:
        with NetworkIsolationGuard(allowed_ports={server.server_port}):
            configs={s:ServiceConfig(f"http://127.0.0.1:{server.server_port}/{s}",api_key="fixture-key") for s in state.versions}
            yield ServiceQueryAdapter(configs,**options),state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def posts(state):return [r for r in state.calls if r["method"]=="POST"]
def adapter(native,**kwargs):return NativeArrWriteAdapter(native,clock=lambda:NOW,**kwargs)
def prepare(controller,call=MOVIE):
    result=controller.execute(call,OWNER)
    assert result.status=="confirmation_required",result
    return result


@pytest.mark.parametrize("call,service,body",[
    (TV,"sonarr",{"name":"EpisodeSearch","episodeIds":[1,7]}),
    (Call(TV.name,{**TV.arguments,"season":1}),"sonarr",{"name":"EpisodeSearch","episodeIds":[1]}),
    (Call(TV.name,{**TV.arguments,"season":0}),"sonarr",{"name":"EpisodeSearch","episodeIds":[6]}),
    (MOVIE,"radarr",{"name":"MoviesSearch","movieIds":[2]}),
    (MUSIC,"lidarr",{"name":"AlbumSearch","albumIds":[8]}),
    (ARTIST,"lidarr",{"name":"AlbumSearch","albumIds":[8,13]}),
])
def test_confirmed_exact_target_commands_remain_dispatched(call,service,body):
    with arr_fixture() as (native,state):
        controller=adapter(native)
        assert controller.supports(call.name)
        pending=prepare(controller,call)
        assert not posts(state) and pending.data["resolved_entity"]["command"]==body
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="dispatched" and not result.verified_complete
        assert result.data["command_scope_correlated"] is True and result.data["search_job_status"]=="queued"
        assert result.data["media_download_completion_verified"] is False
        assert len(posts(state))==1 and posts(state)[0]["body"]==body and posts(state)[0]["service"]==service
        assert controller.confirm_write(pending.data["confirm_id"],OWNER).status=="denied"
        assert len(posts(state))==1
        assert "/fixture/private" not in json.dumps(result.data) and "fixture-key" not in json.dumps(result.data)


@pytest.mark.parametrize("call,query",[
    (TV,'Search for missing episodes of "Fixture series" in Sonarr'),
    (MOVIE,'Search Radarr and download the 2020 movie "Fixture movie"'),
    (MUSIC,'Download the album "Fixture album" by "Fixture artist" using Lidarr'),
    (ARTIST,'Search all missing albums by "Fixture artist" in Lidarr'),
])
def test_scripted_nl_route_executes_real_native_http_after_hmac_confirmation(call,query):
    class RecordingModel:
        family="hermes"
        def __init__(self):self.calls=[]
        def infer(self,utterance,context,max_tokens):
            self.calls.append(utterance);return {"raw":encode(call,"hermes")}
    with arr_fixture() as (native,state):
        model=RecordingModel();runtime=ArrmesRuntimeService(model=model,executor=BoundedExecutor(adapter(native)))
        result=runtime.execute(query,OWNER)
        assert result["terminal_state"]=="awaiting_confirmation" and model.calls==[query]
        assert not posts(state)
        confirmed=runtime.confirm(result["observation"]["data"]["confirm_id"],OWNER)
        assert confirmed["terminal_state"]=="dispatched" and confirmed["proposal"]==normalized(call)
        assert len(posts(state))==1


@pytest.mark.parametrize("mode",["disabled","shadow"])
@pytest.mark.parametrize("call",[TV,MOVIE,MUSIC])
def test_disabled_and_shadow_never_mutate(mode,call):
    with arr_fixture() as (native,state):
        result=adapter(native,write_mode=mode).execute(call,OWNER)
        assert result.status==("denied" if mode=="disabled" else "shadow_logged") and not posts(state)
        if mode=="disabled":assert not state.calls


def test_direct_mode_not_supported_and_read_scope_cannot_prepare_or_confirm():
    with arr_fixture() as (native,state):
        with pytest.raises(ValueError):adapter(native,write_mode="direct")
        controller=adapter(native)
        assert controller.execute(MOVIE,Principal("reader",frozenset({"media:read"}))).status=="denied"
        assert not state.calls
        pending=prepare(controller)
        count=len(state.calls)
        assert controller.confirm_write(pending.data["confirm_id"],Principal(OWNER.subject,frozenset())).status=="denied"
        assert controller.confirm_write(pending.data["confirm_id"],Principal("other",OWNER.scopes)).status=="denied"
        assert len(state.calls)==count and not posts(state)


@pytest.mark.parametrize("field",["arguments","entity","expiry"])
def test_hmac_binds_arguments_target_ids_and_expiry(field):
    with arr_fixture() as (native,state):
        controller=adapter(native);pending=prepare(controller)
        token=controller.confirmation_store._tokens[pending.data["confirm_id"].split(":",1)[1]]
        if field=="arguments":token.arguments["year"]=1980
        elif field=="entity":token.resolved_entity["target_ids"]=[999]
        else:token.expires_at+=100
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="denied" and result.data["error"]=="confirmation_signature_invalid" and not posts(state)


def test_expired_and_foreign_namespace_confirmations_never_dispatch():
    with arr_fixture() as (native,state):
        controller=adapter(native);pending=prepare(controller)
        assert controller.confirm_write(pending.data["confirm_id"],OWNER,now=pending.data["expires_at"]).status=="denied"
        assert controller.confirm_write("native-bazarr:fake",OWNER).status=="denied"
        token=controller.confirmation_store.mint(OWNER,Call("request_media",{"title":"Fixture movie","media_type":"movie"}),summary="other action")
        assert controller.confirm_write("native-arr:"+token.confirm_id,OWNER).status=="denied"
        assert not posts(state)


@pytest.mark.parametrize("mutation",["version","restart","movie_id","title","year","path","file_state","monitoring"])
def test_fresh_movie_identity_and_service_binding_prevent_changed_dispatch(mutation):
    with arr_fixture() as (native,state):
        controller=adapter(native);pending=prepare(controller)
        if mutation=="version":state.versions["radarr"]="6.4.5.10686"
        elif mutation=="restart":state.starts["radarr"]="2026-01-02T00:00:00Z"
        elif mutation=="movie_id":state.movies[0]["id"]=99
        elif mutation=="title":state.movies[0]["title"]="Changed movie"
        elif mutation=="year":state.movies[0]["year"]=1980
        elif mutation=="path":state.movies[0]["path"]="/fixture/private/replaced"
        elif mutation=="file_state":state.movies[0]["hasFile"]=True
        else:state.movies[0]["monitored"]=False
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="denied" and not posts(state)
        assert controller.confirm_write(pending.data["confirm_id"],OWNER).status=="denied"


@pytest.mark.parametrize("mutation",["file","monitoring","new_target","newly_aired","parent_id"])
def test_episode_eligibility_and_exact_target_set_are_rechecked(mutation):
    with arr_fixture() as (native,state):
        controller=adapter(native);pending=prepare(controller,TV)
        if mutation=="file":state.episodes[0]["hasFile"]=True
        elif mutation=="monitoring":state.episodes[0]["monitored"]=False
        elif mutation=="new_target":state.episodes.append(episode(99))
        elif mutation=="newly_aired":state.episodes[1]["airDateUtc"]="2020-01-01T00:00:00Z"
        else:state.episodes[0]["seriesId"]=99
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status in {"denied","error"} and not posts(state)


@pytest.mark.parametrize("mutation",["artist_id","artist_path","album_id","album_name","track_files","release_date","monitored"])
def test_artist_album_identity_and_missing_track_set_are_rechecked(mutation):
    with arr_fixture() as (native,state):
        controller=adapter(native);pending=prepare(controller,ARTIST)
        if mutation=="artist_id":state.artists[0]["id"]=99
        elif mutation=="artist_path":state.artists[0]["path"]="/fixture/private/changed"
        elif mutation=="album_id":state.albums[0]["id"]=99
        elif mutation=="album_name":state.albums[0]["title"]="Changed album"
        elif mutation=="track_files":state.albums[0]["statistics"]["trackFileCount"]=10
        elif mutation=="release_date":state.albums[0]["releaseDate"]="2021-01-01T00:00:00Z"
        else:state.albums[0]["monitored"]=False
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status in {"denied","error"} and not posts(state)


def test_complete_artist_inventory_is_explicit_noop_not_an_empty_or_global_command():
    with arr_fixture() as (native,state):
        state.albums=[album(8,statistics={"trackCount":10,"trackFileCount":10})]
        result=adapter(native).execute(ARTIST,OWNER)
        assert result.status=="verified_ok" and result.verified_complete
        assert result.data["search_dispatched"] is False and result.data["resolved_entity"]["target_ids"]==[]
        assert not posts(state) and "confirm_id" not in result.data


@pytest.mark.parametrize("call",[Call(TV.name,{**TV.arguments,"season":99}),Call(MUSIC.name,{**MUSIC.arguments,"album":"Future album"})])
def test_empty_or_unreleased_exact_scope_clarifies_before_confirmation(call):
    with arr_fixture() as (native,state):
        result=adapter(native).execute(call,OWNER)
        assert result.status=="clarify_required" and not posts(state)


@pytest.mark.parametrize("service,version,call",[("sonarr","3.0.0.0",TV),("radarr","5.27.0.0",MOVIE),("lidarr","2.14.0.0",MUSIC)])
def test_unaudited_major_versions_are_refused(service,version,call):
    with arr_fixture() as (native,state):
        state.versions[service]=version
        result=adapter(native).execute(call,OWNER)
        assert result.status=="error" and result.data["error"]=="unsupported_arr_service_version" and not posts(state)


@pytest.mark.parametrize("fault",["duplicate_id","duplicate_title","paginated","malformed_json","wrong_parent","bad_airdate","bad_album_stats","missing_path","wrong_app"])
def test_malformed_incomplete_and_ambiguous_inventory_does_not_produce_write(fault):
    fixture=ArrFixtureState();call=TV
    if fault=="duplicate_id":fixture.series.append({**fixture.series[0],"title":"Other title"})
    elif fault=="duplicate_title":fixture.series.append({**fixture.series[0],"id":99})
    elif fault=="paginated":fixture.faults[("sonarr","/api/v3/series")]=(200,{"records":fixture.series,"totalRecords":99})
    elif fault=="malformed_json":fixture.faults[("sonarr","/api/v3/series")]=(200,b'[{"id":1,"id":2}]')
    elif fault=="wrong_parent":fixture.episodes.append(episode(99,seriesId=999))
    elif fault=="bad_airdate":fixture.episodes[0]["airDateUtc"]="2020-01-01"
    elif fault=="bad_album_stats":fixture.albums[0]["statistics"]["trackFileCount"]=False;call=ARTIST
    elif fault=="missing_path":fixture.series[0]["path"]=None
    else:fixture.faults[("sonarr","/api/v3/system/status")]=(200,{"appName":"Radarr","version":"4.0.20.3014"})
    with arr_fixture(fixture) as (native,state):
        result=adapter(native).execute(call,OWNER)
        assert result.status in {"error","clarify_required"} and not posts(state) and "confirm_id" not in result.data


def test_unfiltered_catalogue_scanned_before_uniqueness_and_target_cap_does_not_truncate():
    with arr_fixture(max_records=1) as (native,state):
        state.movies.append({**state.movies[0],"id":99})
        assert adapter(native).execute(MOVIE,OWNER).status=="clarify_required"
        assert adapter(native).execute(TV,OWNER).status=="clarify_required"
        assert not posts(state)


@pytest.mark.parametrize("field,value",[("id",True),("id",2**31),("seriesId",True),("seriesId",1.0)])
def test_api_id_tokens_are_positive_int32_not_boolean_or_integral_float(field,value):
    with arr_fixture() as (native,state):
        state.episodes[0][field]=value
        result=adapter(native).execute(TV,OWNER)
        assert result.status=="error" and not posts(state)


@pytest.mark.parametrize("behavior,outcome",[("error","unknown"),("disconnect","unknown"),("redirect","unknown"),
    ("rejected","rejected"),("invalid_json","unknown"),("unexpected200","unknown")])
def test_single_post_failure_never_retries(behavior,outcome):
    with arr_fixture(ArrFixtureState(post_behavior=behavior)) as (native,state):
        controller=adapter(native);pending=prepare(controller)
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="error" and result.data["outcome"]==outcome and not result.verified_complete
        assert len(posts(state))==1 and sum(r["method"]=="POST" for r in result.audit_trail)==1
        assert controller.confirm_write(pending.data["confirm_id"],OWNER).status=="denied" and len(posts(state))==1


@pytest.mark.parametrize("behavior",["empty202","wrong_name","missing_scope"])
def test_uncorrelatable_acceptance_stays_dispatched(behavior):
    with arr_fixture(ArrFixtureState(post_behavior=behavior)) as (native,state):
        controller=adapter(native);pending=prepare(controller)
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="dispatched" and not result.verified_complete and not result.data["command_scope_correlated"]
        assert not result.data["media_download_completion_verified"] and len(posts(state))==1


@pytest.mark.parametrize("behavior",["wrong_id","wrong_name","wrong_targets","extra_scope","unavailable"])
def test_post_dispatch_job_observation_must_correlate_exact_command_id_and_targets(behavior):
    with arr_fixture(ArrFixtureState(observation_behavior=behavior,job_status="completed",job_result="successful")) as (native,state):
        controller=adapter(native);pending=prepare(controller)
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="dispatched" and not result.data["search_job_completion_verified"] and not result.verified_complete
        assert len(posts(state))==1


def test_correlated_successful_search_job_is_separate_from_media_file_completion():
    with arr_fixture(ArrFixtureState(job_status="completed",job_result="successful")) as (native,state):
        controller=adapter(native);pending=prepare(controller)
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.data["command_scope_correlated"] and result.data["search_job_completion_verified"]
        assert result.status=="dispatched" and not result.verified_complete
        assert not result.data["media_download_completion_verified"]


def test_unknown_command_status_is_not_treated_as_completion():
    with arr_fixture(ArrFixtureState(job_status=[])) as (native,state):
        controller=adapter(native);pending=prepare(controller)
        result=controller.confirm_write(pending.data["confirm_id"],OWNER)
        assert result.status=="dispatched" and not result.data["search_job_completion_verified"]


def test_unavailable_service_and_unsigned_ids_fail_before_post():
    with arr_fixture() as (native,state):
        state.faults[("radarr","/api/v3/movie")]=(503,{})
        assert adapter(native).execute(MOVIE,OWNER).status=="error" and not posts(state)
        with pytest.raises(InvalidCall):adapter(native).execute(Call("movie_search",{**MOVIE.arguments,"movieIds":[999]}),OWNER)
        with pytest.raises(InvalidCall):adapter(native).execute(Call("tv_search_missing",{**TV.arguments,"season":False}),OWNER)
