"""Phase 6e — Records / Case-Management layer.

This package turns the platform from an advice-giver that mints throwaway
ticket IDs into a system of record that does real government casework:

    register → persist → route → escalate → track → feedback → close/reopen

Modelled on MP CM Helpline (181) escalation, UP Jansunwai (IGRS) intake +
public tracking, and the National Consumer Helpline (INGRAM) "route to a
responsible desk and chase it" pattern.

Every trackable thing — grievances, scheme applications, development-project
subscriptions, service requests — is one `Record` with a `kind` discriminator
(see records/models.py). The lifecycle FSM, SLA timers, and L1→L4 escalation
matrix are shared across all kinds.
"""
from __future__ import annotations

from .models import Record, TimelineEvent, RecordKind
from .store import records_store
from . import service

__all__ = ["Record", "TimelineEvent", "RecordKind", "records_store", "service"]
