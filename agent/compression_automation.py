"""Carry automation at a compression boundary without opening worker storage."""


def carry_compression_automation(agent, old_session_id):
    from agent.runtime_session_store import RuntimeSessionStore
    if isinstance(agent._session_db, RuntimeSessionStore):
        # The owner moved these exact sidecars atomically with child publication.
        return
    from agent.conversation_compression import _swallow
    with _swallow('Could not migrate goal on compression: %s'):
        # A refused transfer must not be silent: the goal stays on the parent
        # session, so the user is told instead of watching it disappear (W2).
        from agent.conversation_compression_goal_carry import carry_goal_to_child
        carry_goal_to_child(agent, old_session_id)
    with _swallow('Could not migrate heartbeat on compression: %s'):
        from hermes_cli.heartbeat import migrate_heartbeat_to_session
        migrate_heartbeat_to_session(old_session_id, agent.session_id)
    with _swallow('Could not migrate loop on compression: %s'):
        from hermes_cli.loops import migrate_loop_to_session
        migrate_loop_to_session(old_session_id, agent.session_id, reason='compression')
