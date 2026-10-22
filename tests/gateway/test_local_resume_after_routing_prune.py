"""A mounted local conversation remains attachable after idle routing-cache eviction."""
from datetime import timedelta
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_aged_local_session_can_reattach_after_route_prune(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_local import local_receipt

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='first')
    actor = Principal('owner', 'fixture', frozenset({'session:create', 'session:read'}), 'socket')
    ref = create_local_session(authority, actor, {'request_id': 'old-chat', 'source': 'gui',
        'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
    db = authority.db
    db.append_message(ref.session_id, 'user', 'kept transcript')
    db.end_session(ref.session_id, 'startup_orphan_reap')
    before = db.get_session(ref.session_id)
    receipt = local_receipt(db, ref.session_id)
    messages = db.get_messages(ref.session_id)
    route = authority.sessions[ref.session_id].route
    store._entries[route].updated_at -= timedelta(days=91)
    try:
        assert restore_local_session(authority, ref.session_id) == ref
        removed = store.prune_old_entries(90)
        print('routing prune removed:', removed, 'mounted owner survives:', ref.session_id in authority.sessions)
        first = await authority.attach(actor, ref)
        await authority.detach(actor, first.subscription_id)
        second = await authority.attach(actor, ref)
        assert second.handle.ref == ref
        assert [row['content'] for row in second.history] == ['kept transcript']
        assert db.get_messages(ref.session_id) == messages
        assert local_receipt(db, ref.session_id) == receipt
        after = db.get_session(ref.session_id)
        assert (after['ended_at'], after['end_reason']) == (before['ended_at'], 'startup_orphan_reap')
        assert db.session_count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_resume_survives_repeated_prunes_and_new_connections(tmp_path, monkeypatch):
    from gateway.session_controls import AuthorityConnection
    from tests.gateway.test_local_route_orphan_reap_stamp import _local_session

    store, authority, first, ref = await _local_session(tmp_path, monkeypatch)
    connections = [first]
    route = authority.sessions[ref.session_id].route
    store._entries[route].updated_at -= timedelta(days=91)
    authority.db.end_session(ref.session_id, 'startup_orphan_reap')
    before = authority.db.get_messages(ref.session_id)
    try:
        for index in range(3):
            store.prune_old_entries(90)
            connection = first if index == 0 else AuthorityConnection(authority, object(), {'user_id': 'owner'})
            if index:
                connections.append(connection)
            reply = await connection.dispatch({'id': index + 1, 'method': 'session.resume',
                                               'params': {'session_id': ref.session_id}})
            assert 'result' in reply, reply
            assert reply['result']['session_id'] == ref.session_id
            assert [m['content'] for m in reply['result']['messages']] == ['earlier turn']
            assert authority.db.get_session(ref.session_id)['end_reason'] == 'startup_orphan_reap'
        assert authority.db.get_messages(ref.session_id) == before
        assert authority.db.session_count() == 1
    finally:
        for connection in connections:
            await connection.close()
        authority.db.close()


@pytest.mark.asyncio
async def test_repointed_local_route_with_history_is_still_refused(tmp_path, monkeypatch):
    from tests.gateway.test_local_route_orphan_reap_stamp import _local_session

    store, authority, connection, ref = await _local_session(tmp_path, monkeypatch)
    route = authority.sessions[ref.session_id].route
    try:
        authority.db.create_session('other-history', 'local', session_key=route,
                                    chat_id=ref.session_id, user_id='owner')
        authority.db.append_message('other-history', 'user', 'not this conversation')
        store._entries[route].session_id = 'other-history'
        reply = await connection.dispatch({'id': 1, 'method': 'session.resume',
                                           'params': {'session_id': ref.session_id}})
        assert reply['error']['data']['reason'] == 'admission_conflict', reply
        assert store._entries[route].session_id == 'other-history'
        assert authority.db.session_count() == 2
    finally:
        await connection.close()
        authority.db.close()
