"""One-time migration of pre-membership agent participation."""

from sqlalchemy import text


def backfill(connection):
    marker = "20261002_legacy_agent_channels"
    if connection.execute(
        text("SELECT name FROM workspace_data_migrations WHERE name=:name"), {"name": marker}
    ).first():
        return
    # Only agents with no explicit membership need adoption. Preserve admin removals.
    connection.execute(
        text("""
        INSERT INTO workspace_agent_channels (org_id, agent_id, channel_id, joined_at)
        SELECT DISTINCT e.org_id, e.agent_id, e.channel_id, CURRENT_TIMESTAMP
        FROM (
            SELECT org_id, sender_id AS agent_id, room_id AS channel_id
            FROM workspace_messages WHERE sender_kind = 'agent'
            UNION
            SELECT org_id, agent_id, channel_id FROM workspace_agent_checkins
            UNION
            SELECT org_id, agent_id, channel_id FROM workspace_session_presence
            WHERE agent_id IS NOT NULL AND channel_id IS NOT NULL
        ) e
        JOIN workspace_agents a ON a.org_id=e.org_id AND a.id=e.agent_id
        JOIN workspace_rooms r ON r.org_id=e.org_id AND r.id=e.channel_id
        WHERE a.active = true
          AND NOT EXISTS (SELECT 1 FROM workspace_agent_channels m
                          WHERE m.org_id=e.org_id AND m.agent_id=e.agent_id)
          AND NOT EXISTS (SELECT 1 FROM workspace_audit v
                          WHERE v.org_id=e.org_id AND v.action='agent.channel_removed'
                          AND v.target_id=e.agent_id || ':' || e.channel_id)
    """)
    )
    connection.execute(
        text("INSERT INTO workspace_data_migrations (name) VALUES (:name)"), {"name": marker}
    )
