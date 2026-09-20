"""
payments_service -- the *target system* that the SRE Agent Crew investigates.

A small, fake payments API (no real money, no real cards) that behaves like
a real production service: it emits structured logs, Prometheus metrics and
request traces, has a deploy history, and -- crucially -- can be made to
genuinely misbehave via fault injection. The agents investigate what this
service actually emitted, instead of data written to match a known answer.
"""
