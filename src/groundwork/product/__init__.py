"""Phase 3 product layer: Slack interface, failure diagnosis, context-fix proposals, GitHub PRs,
human approvals, audit trail, context deployment, shadow mode, pilot measurement.

Everything here sits ON TOP of the Phase 2 agent (``groundwork.agent``) and the Phase 1 service.
No module in this package opens the serving warehouse for question answering: data access stays
agent -> MCP -> GroundworkService -> SQL guard -> read-only worker.
"""
