"""Leaner inventory: stopped containers, sidecars, firmware/cards, ignoring, duplicate merging."""
import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from ripe.app import create_app
from ripe.autoprofile import link_sidecars
from ripe.config import Runtime
from ripe.models import AppRecord, Release, Settings, hidden, utcnow
from ripe.service import Coordinator
from ripe.sources import collector
from ripe.sources.collector import ha_records, merge_ha_duplicates


def container(name, image, **facts):
    app = AppRecord(id="docker:" + name, name=name, source="docker", image=image, state="running")
    base = {"group": "", "networks": {}, "env_values": {}, "links": []}
    return app, {**base, **facts}


def link(*items):
    apps = [a for a, _ in items]
    link_sidecars(apps, {a.id: f for a, f in items})
    return {a.name: a.parent_id for a in apps}


def test_include_stopped_defaults_off():
    assert Settings().include_stopped is False


def test_hidden_rules():
    off, on = Settings(), Settings(include_stopped=True)
    stopped = AppRecord(id="docker:old", name="old", source="docker", state="exited")
    running = AppRecord(id="docker:new", name="new", source="docker", state="running")
    assert hidden(stopped, off, {}) and not hidden(stopped, on, {}) and not hidden(running, off, {})
    assert hidden(running.model_copy(update={"ignored": True}), on, {})
    side = running.model_copy(update={"id": "docker:db", "parent_id": stopped.id})
    assert hidden(side, off, {stopped.id: stopped}) and not hidden(side, on, {stopped.id: stopped})


def test_sidecar_by_name_prefix_and_compose_project():
    parent, pf = container("dawarich_app", "freikin/dawarich:1")
    db, df = container("dawarich_db", "postgis/postgis:14")
    cache, cf = container("dawarich_redis", "redis:7")
    result = link((parent, pf), (db, df), (cache, cf))
    assert result == {"dawarich_app": None, "dawarich_db": parent.id, "dawarich_redis": parent.id}
    assert "name" in db.detected["sidecar_reason"]
    other, of = container("unrelated", "x/y:1", group="stack")
    pg, gf = container("pg", "postgres:16", group="stack")
    assert link((other, of), (pg, gf))["pg"] == other.id


def test_sidecar_by_name_containing_parent():
    immich, a = container("immich", "ghcr.io/immich-app/immich-server:v1")
    pg, b = container("PostgreSQL_Immich", "tensorchord/pgvecto-rs:14")
    chroma, c = container("odysseus-chromadb", "chromadb/chroma:0.5")
    odysseus, d = container("Odysseus", "x/odysseus:1")
    result = link((immich, a), (pg, b), (chroma, c), (odysseus, d))
    assert result["PostgreSQL_Immich"] == immich.id and result["odysseus-chromadb"] == odysseus.id


def test_sidecar_by_env_reference_beats_name():
    app, af = container("web", "x/web:1", env_values={"DB_HOST": "store"})
    other, of = container("worker", "x/worker:1")
    db, df = container("store", "mariadb:11")
    assert link((app, af), (other, of), (db, df))["store"] == app.id


def test_sidecar_by_small_shared_network_and_shared_datastore():
    app, af = container("tool", "x/tool:1", networks={"tool_net": "1.1.1.2"})
    db, df = container("data", "mysql:8", networks={"tool_net": "1.1.1.3"})
    assert link((app, af), (db, df))["data"] == app.id
    # A datastore that two apps name in their settings serves both, so it stays on its own.
    a, a_f = container("a", "x/a:1", env_values={"DB_HOST": "shared"})
    b, b_f = container("b", "x/b:1", env_values={"DB_HOST": "shared"})
    shared, s_f = container("shared", "postgres:16")
    assert link((a, a_f), (b, b_f), (shared, s_f))["shared"] is None


def test_non_database_image_is_never_a_sidecar():
    app, af = container("app_main", "x/app:1", group="s")
    worker, wf = container("app_worker", "x/worker:1", group="s")
    assert link((app, af), (worker, wf)) == {"app_main": None, "app_worker": None}


def ha_state(entity, title, installed, latest, **attrs):
    return {"entity_id": entity, "state": "on", "attributes": {"title": title, "installed_version": installed, "latest_version": latest, **attrs}}


def test_firmware_and_cards_sections():
    states = [ha_state("update.plug_firmware", "Plug Firmware", "1", "2"),
              ha_state("update.sensor", "Sensor", "1", "2", device_class="firmware"),
              ha_state("update.mushroom", "Mushroom Cards Update", "1", "2"),
              ha_state("update.core", "Home Assistant Core Update", "1", "2")]
    apps, _ = ha_records(states, {}, {})
    assert [a.section for a in apps] == ["firmware", "firmware", "cards", None]


def test_duplicate_ha_entities_merge_and_same_names_disambiguate():
    url = "https://github.com/acme/ha-mcp/releases/tag/v2"
    states = [ha_state("update.ha_mcp_a", "HA-MCP Custom Component Update", "1", "2", release_url=url),
              ha_state("update.ha_mcp_b", "HA-MCP Custom Component Update", "1", "2", release_url=url),
              ha_state("update.bulb_1", "Bulb firmware", "1", "2", release_url=url, device_class="firmware"),
              ha_state("update.bulb_2", "Bulb firmware", "1", "2", release_url=url, device_class="firmware")]
    apps, _ = ha_records(states, {}, {})
    merge_ha_duplicates(apps)
    by_id = {a.id: a for a in apps}
    assert by_id["ha:update.ha_mcp_b"].merged_into == "ha:update.ha_mcp_a" and not by_id["ha:update.ha_mcp_a"].merged_into
    assert hidden(by_id["ha:update.ha_mcp_b"], Settings(), by_id)
    assert not by_id["ha:update.bulb_1"].merged_into and not by_id["ha:update.bulb_2"].merged_into
    assert by_id["ha:update.bulb_1"].name == "Bulb firmware (bulb_1)" and by_id["ha:update.bulb_2"].name == "Bulb firmware (bulb_2)"


def test_shared_repo_with_different_versions_is_not_merged():
    url = "https://github.com/acme/monorepo/releases/tag/v2"
    states = [ha_state("update.addon_a", "Add-on A Update", "1", "2", release_url=url),
              ha_state("update.addon_b", "Add-on B Update", "5", "6", release_url=url)]
    apps, _ = ha_records(states, {}, {})
    merge_ha_duplicates(apps)
    assert not any(a.merged_into for a in apps)


# --- coordinator / API -------------------------------------------------------------------------

def runtime(tmp_path):
    return Runtime(config_dir=tmp_path, disable_scheduler=True, demo=False)


def make(tmp_path):
    manager = Coordinator(runtime(tmp_path))
    return manager


def add(manager, app_id, **fields):
    app = AppRecord(id=app_id, name=fields.pop("name", app_id), source=fields.pop("source", "docker"), **fields)
    manager.store.save_app(app)
    return app


def release(app_id, version, **fields):
    return Release(id=f"{app_id}:{version}", app_id=app_id, version=version, published_at=utcnow() - timedelta(days=60), notes="x" * 80, **fields)


def test_stopped_hidden_by_default_counted_and_shown_when_included(tmp_path):
    manager = make(tmp_path)
    add(manager, "docker:run", state="running", installed_version="1", latest_version="1")
    add(manager, "docker:off", state="exited", installed_version="1", latest_version="1")
    data = manager.dashboard()
    assert [a["app"]["id"] for a in data["apps"]] == ["docker:run"] and data["stopped_hidden"] == 1
    settings = manager.settings().model_copy(update={"include_stopped": True})
    manager.store.save_settings(settings)
    data = manager.dashboard()
    assert len(data["apps"]) == 2 and data["stopped_hidden"] == 0
    manager.close()


def test_stopped_sidecar_is_counted_separately_from_its_parent(tmp_path):
    manager = make(tmp_path)
    add(manager, "docker:app", state="running", installed_version="1", latest_version="1")
    add(manager, "docker:app_db", state="exited", parent_id="docker:app", installed_version="1", latest_version="1")
    data = manager.dashboard()
    assert data["stopped_hidden"] == 1 and data["apps"][0]["sidecars"] == []
    manager.close()


def test_ignore_app_and_version_with_undo(tmp_path):
    manager = make(tmp_path)
    manager.request_poll = lambda: False  # no event loop in this test
    add(manager, "docker:x", installed_version="1.0.0", latest_version="1.1.0")
    manager.store.save_release(release("docker:x", "1.1.0"))
    assert manager.dashboard()["apps"][0]["app"]["id"] == "docker:x"
    manager.set_ignore("docker:x", "version", True)
    data = manager.dashboard()
    assert data["apps"] == [] and data["ignored"] == [{"id": "docker:x", "name": "docker:x", "scope": "version", "version": "1.1.0"}]
    # A newer release brings it back.
    app = manager.store.app("docker:x")
    app.latest_version = "1.2.0"
    manager.store.save_app(app)
    manager.store.save_release(release("docker:x", "1.2.0"))
    assert len(manager.dashboard()["apps"]) == 1
    manager.set_ignore("docker:x", "version", False)
    manager.set_ignore("docker:x", "app", True)
    data = manager.dashboard()
    assert data["apps"] == [] and data["ignored"][0]["scope"] == "app"
    manager.store.set("poll_again", False)
    manager.set_ignore("docker:x", "app", False)
    assert len(manager.dashboard()["apps"]) == 1 and manager.dashboard()["ignored"] == []
    manager.close()


def test_ignore_api(tmp_path):
    with TestClient(create_app(runtime(tmp_path)), headers={"X-Ripe-Request": "1", "Host": "localhost"}) as client:
        manager = client.app.state.manager
        add(manager, "docker:x", installed_version="1.0.0", latest_version="1.1.0")
        manager.store.save_release(release("docker:x", "1.1.0"))
        assert client.post("/api/apps/docker:x/ignore", json={"scope": "bogus"}).status_code == 422
        assert client.post("/api/apps/docker:nope/ignore", json={"scope": "app"}).status_code == 404
        add(manager, "docker:fresh", installed_version="1.0.0")
        assert client.post("/api/apps/docker:fresh/ignore", json={"scope": "version"}).status_code == 409
        assert client.post("/api/apps/docker:x/ignore", json={"scope": "version"}).json()["ignored_version"] == "1.1.0"
        assert client.delete("/api/apps/docker:x/ignore?scope=version").json()["ignored_version"] is None
        assert client.post("/api/apps/docker:x/ignore", json={"scope": "app"}).json()["ignored"] is True
        assert client.get("/api/dashboard").json()["ignored"][0]["scope"] == "app"
        polls = []
        manager.request_poll = lambda: polls.append(1) or True
        assert client.patch("/api/settings", json={"include_stopped": True}).json()["include_stopped"] is True
        assert polls == [1]
        client.patch("/api/settings", json={"include_stopped": True})
        assert polls == [1]  # no change, no extra check


def test_sidecars_fold_into_parent_and_raise_its_status(tmp_path):
    manager = make(tmp_path)
    add(manager, "docker:app", name="app", state="running", installed_version="2.0.0", latest_version="2.0.0")
    add(manager, "docker:app_db", name="app_db", state="running", parent_id="docker:app", installed_version="16.1.0", latest_version="16.2.0")
    sec = release("docker:app_db", "16.2.0", classification={"status": "ok", "tags": ["security"], "security": [{"description": "d", "evidence": "e"}]},
                  applicability=[{"kind": "security", "index": 0, "status": "applies", "evidence": "e", "profile_evidence": "p", "verified": True}])
    manager.store.save_release(sec)
    data = manager.dashboard()
    assert [a["app"]["id"] for a in data["apps"]] == ["docker:app"]
    card = data["apps"][0]
    assert [s["app"]["id"] for s in card["sidecars"]] == ["docker:app_db"]
    assert card["status"] == "recommended_now" and "app_db" in card["reason"]
    assert data["summary"]["recommended_now"] == 1
    assert [s.app.id for s in manager.view("docker:app").sidecars] == ["docker:app_db"]
    manager.close()


def test_ignored_parent_version_keeps_sidecar_updates_visible(tmp_path):
    manager = make(tmp_path)
    add(manager, "docker:app", name="app", state="running", installed_version="1.0.0", latest_version="1.1.0", ignored_version="1.1.0")
    add(manager, "docker:app_db", name="app_db", state="running", parent_id="docker:app", installed_version="16.1.0", latest_version="16.2.0")
    manager.store.save_release(release("docker:app", "1.1.0"))
    manager.store.save_release(release("docker:app_db", "16.2.0", classification={"status": "ok", "tags": ["security"], "security": [{"description": "d", "evidence": "e"}]},
                  applicability=[{"kind": "security", "index": 0, "status": "applies", "evidence": "e", "profile_evidence": "p", "verified": True}]))
    card = manager.dashboard()["apps"][0]
    assert card["app"]["id"] == "docker:app" and card["status"] == "recommended_now"
    assert [s["app"]["id"] for s in card["sidecars"]] == ["docker:app_db"]
    manager.close()


def test_unknown_sidecar_status_reaches_up_to_date_parent(tmp_path):
    manager = make(tmp_path)
    add(manager, "docker:app", name="app", state="running", installed_version="2.0.0", latest_version="2.0.0")
    add(manager, "docker:app_db", name="app_db", state="running", parent_id="docker:app", installed_version="16.1.0", latest_version="16.2.0")
    card = manager.dashboard()["apps"][0]
    assert card["sidecars"][0]["status"] == "unknown" and card["status"] == "unknown"
    manager.close()


async def test_firmware_without_notes_never_reaches_a_model(tmp_path, monkeypatch):
    manager = make(tmp_path)
    app = add(manager, "ha:update.fw", source="home_assistant", section="firmware", installed_version="1.0.0", latest_version="1.1.0")
    manager.store.save_release(Release(id="ha:fw:1.1.0", app_id=app.id, version="1.1.0", notes=""))

    async def boom(*args, **kwargs):
        raise AssertionError("model must not be called")

    monkeypatch.setattr("ripe.service.build_rollup", boom)

    async def ready():
        raise AssertionError("model must not be probed")

    await manager.refresh_rollup(app, manager.settings(), ready)
    manager.close()
