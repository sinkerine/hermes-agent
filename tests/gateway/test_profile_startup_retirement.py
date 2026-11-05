"""Profile ownership covers startup threads and recovery, including a cancelled observer."""
import asyncio
import threading

import pytest
import pytest_asyncio

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    from gateway.control_socket import GatewayControlServer
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=('alpha',))
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    authorities = []
    server = runner.session_control_server = GatewayControlServer(root)
    try:
        await initialize_gateway_runtime(runner)
        authorities = list(runner.session_authorities)
        assert await server.start()
        yield runner, homes[1][1]
    finally:
        for authority in authorities:
            service = getattr(authority, 'hosted_room_service', None)
            if service is not None:
                assert await asyncio.to_thread(service.stop, timeout=5)
        await server.stop()
        runner.session_store.close_all_db_handles()
        for _, home in homes:
            process_ownership.release(home)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['import', 'constructor'])
@pytest.mark.parametrize('cancel_observer', [False, True])
async def test_retirement_keeps_ownership_until_hosted_preparation_finishes(
        runtime, monkeypatch, phase, cancel_observer):
    from gateway import hosted_rooms_gateway_import, session_hosted_service
    from gateway.run_runtime import _retire_profile_authority, unserve_profile_runtime
    runner, home = runtime
    authority = runner.session_authorities.require(home)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    module, name = ((hosted_rooms_gateway_import, 'import_standalone_rooms') if phase == 'import'
                    else (session_hosted_service, 'CanonicalHostedRoomService'))
    real = getattr(module, name)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test never released hosted preparation'
        try:
            return real(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(module, name, held)
    observer = asyncio.create_task(session_hosted_service._ensure_hosted_service(runner, authority))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if cancel_observer:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        retired = await _retire_profile_authority(authority, timeout=0)
        assert retired is False, 'ownership was releasable while its constructor could still write'
        assert not finished.is_set()
        assert runner.session_authorities.require(home) is authority
    finally:
        release.set()
        await asyncio.gather(observer, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 10)
        assert await unserve_profile_runtime(runner, home)
    service = authority.hosted_room_service
    assert not service.runtime.status()['running']
    authority.db.close()
    moved = home.with_name('renamed')
    home.rename(moved)
    await session_hosted_service._ensure_hosted_service(runner, authority)
    assert not home.exists(), 'a stale preparation request recreated the retired home'
    assert (moved / 'config.yaml').is_file()


@pytest.mark.asyncio
async def test_boot_recovery_does_not_restore_an_authority_retired_during_hosted_setup(runtime, monkeypatch):
    from gateway import run_runtime, session_hosted_service, session_local_recovery
    runner, home = runtime
    authority = runner.session_authorities.require(home)
    entered, resume = asyncio.Event(), asyncio.Event()
    real_ensure = session_hosted_service.ensure_hosted_service
    restored = []
    real_recover = session_local_recovery.recover_local_sessions

    async def paused(runner):
        entered.set()
        await resume.wait()
        return await real_ensure(runner)

    def record(owner, **kwargs):
        restored.append(owner.profile_id)
        return real_recover(owner, **kwargs)

    monkeypatch.setattr(session_hosted_service, 'ensure_hosted_service', paused)
    monkeypatch.setattr(session_local_recovery, 'recover_local_sessions', record)
    recovery = asyncio.create_task(run_runtime.recover_gateway_native_sessions(runner))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await run_runtime.unserve_profile_runtime(runner, home)
        authority.db.close()
        home.rename(home.with_name('renamed'))
    finally:
        resume.set()
        await recovery
    assert authority.profile_id not in restored, 'boot restored the stale pre-await authority snapshot'
    assert not home.exists()


@pytest.mark.asyncio
async def test_retirement_joins_the_after_ready_room_import(runtime, monkeypatch):
    from gateway import hosted_rooms_gateway_import, run_runtime, session_hosted_service
    runner, home = runtime
    authority = runner.session_authorities.require(home)
    await session_hosted_service._ensure_hosted_service(runner, authority)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = hosted_rooms_gateway_import.import_standalone_rooms

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test never released ready import'
        try:
            return real(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(hosted_rooms_gateway_import, 'import_standalone_rooms', held)
    hosted_rooms_gateway_import.import_after_ready([authority])
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert await run_runtime._retire_profile_authority(authority, timeout=0) is False
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 10)
        assert await run_runtime.unserve_profile_runtime(runner, home)
    authority.db.close()
    home.rename(home.with_name('renamed'))
    assert not home.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['mailbox', 'native'])
async def test_retirement_joins_recovery_after_its_observer_disconnects(runtime, monkeypatch, operation):
    from gateway import run_runtime, session_bot
    runner, home = runtime
    authority = runner.session_authorities.require(home)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held(*args):
        entered.set()
        await release.wait()
        finished.set()
        return {}

    if operation == 'mailbox':
        monkeypatch.setattr(session_bot, 'recover_bot_deliveries', held)
        recovery = run_runtime.recover_bot_mailbox(authority)
    else:
        monkeypatch.setattr(authority, 'recover_native_sessions', held)
        recovery = run_runtime.recover_profile_native_sessions(runner, authority)
    observer = asyncio.create_task(recovery)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        assert await run_runtime._retire_profile_authority(authority, timeout=0) is False
        assert not finished.is_set()
    finally:
        release.set()
        assert await run_runtime.unserve_profile_runtime(runner, home)
    assert finished.is_set()
    authority.db.close()
    home.rename(home.with_name('renamed'))
    assert not home.exists()
