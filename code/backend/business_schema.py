"""Add durable business records to a v3 database without replacing its data."""

_STAMP = "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
_RUN_FK = "FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id)"
_POLICY_FK = "FOREIGN KEY(facility_id,policy_version) REFERENCES policies(facility_id,policy_version)"


def migrate_business(db):
    """Migrate a v3 connection to v4. SQLite DDL and version move are atomic."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 4:
        return
    if version != 3:
        raise RuntimeError("Business migration requires schema v3")
    if db.in_transaction:
        raise RuntimeError("Business migration requires a fresh transaction")

    statements = [
        f"""CREATE TABLE observation_evidence (
            observation_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            state_version INTEGER NOT NULL CHECK(state_version>=0),
            sim_time_ms INTEGER NOT NULL CHECK(sim_time_ms>=0),
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), digest TEXT NOT NULL,
            created_at {_STAMP}, {_RUN_FK}, UNIQUE(facility_id,run_id,observation_id))""",
        f"""CREATE TABLE incidents (
            incident_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('candidate','active','monitoring','needs_review','escalated','resolved','closed_no_issue','closed_false_positive')),
            primary_object_id TEXT NOT NULL, dedup_key TEXT NOT NULL,
            policy_version INTEGER NOT NULL, reason_summary TEXT NOT NULL,
            previous_incident_id TEXT REFERENCES incidents(incident_id),
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            {_RUN_FK}, {_POLICY_FK}, UNIQUE(facility_id,run_id,incident_id))""",
        """CREATE UNIQUE INDEX incidents_open_dedup ON incidents(facility_id,run_id,dedup_key)
            WHERE status NOT IN ('resolved','closed_no_issue','closed_false_positive')""",
        f"""CREATE TABLE incident_impacts (
            impact_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            incident_id TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('aisle_obstruction')),
            object_id TEXT, zone_id TEXT NOT NULL, condition_json TEXT NOT NULL CHECK(json_valid(condition_json)),
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            {_RUN_FK}, FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id))""",
        f"""CREATE TABLE incident_evidence (
            facility_id TEXT NOT NULL, run_id TEXT NOT NULL, incident_id TEXT NOT NULL,
            observation_id TEXT NOT NULL, analysis_version TEXT NOT NULL,
            metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)), purpose TEXT NOT NULL,
            created_at {_STAMP}, PRIMARY KEY(incident_id,observation_id,purpose), {_RUN_FK},
            FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id),
            FOREIGN KEY(facility_id,run_id,observation_id) REFERENCES observation_evidence(facility_id,run_id,observation_id))""",
        f"""CREATE TABLE commands (
            command_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            requester_id TEXT NOT NULL REFERENCES users(user_id),
            request_text TEXT NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('query','operational_goal','own_vehicle_query','report_exit_blocked')),
            target_vehicle_id TEXT, normalized_goal_json TEXT CHECK(normalized_goal_json IS NULL OR json_valid(normalized_goal_json)),
            aggregate_status TEXT NOT NULL CHECK(aggregate_status IN ('pending','running','succeeded','failed','partial','held','cancelled','unknown')),
            cancellation_requested_at TEXT, created_at {_STAMP}, updated_at {_STAMP},
            resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1), {_RUN_FK},
            FOREIGN KEY(facility_id,target_vehicle_id) REFERENCES vehicles(facility_id,registered_vehicle_id),
            UNIQUE(facility_id,run_id,command_id))""",
        f"""CREATE TABLE followups (
            followup_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            incident_id TEXT, command_id TEXT, requester_ref TEXT NOT NULL,
            clock TEXT NOT NULL CHECK(clock IN ('sim','wall')),
            due_sim_time_ms INTEGER CHECK(due_sim_time_ms>=0), due_at TEXT,
            condition_json TEXT NOT NULL CHECK(json_valid(condition_json)),
            status TEXT NOT NULL CHECK(status IN ('scheduled','claimed','completed','cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 3),
            policy_version INTEGER NOT NULL,
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            CHECK(incident_id IS NOT NULL OR command_id IS NOT NULL),
            CHECK((clock='sim' AND due_sim_time_ms IS NOT NULL AND due_at IS NULL) OR
                  (clock='wall' AND due_at IS NOT NULL AND due_sim_time_ms IS NULL)),
            {_RUN_FK}, {_POLICY_FK},
            FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id),
            FOREIGN KEY(facility_id,run_id,command_id) REFERENCES commands(facility_id,run_id,command_id),
            UNIQUE(facility_id,run_id,followup_id))""",
        f"""CREATE TABLE plans (
            plan_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            incident_id TEXT, command_id TEXT, trigger_followup_id TEXT UNIQUE,
            steps_json TEXT NOT NULL CHECK(json_valid(steps_json)), model_ref TEXT NOT NULL,
            policy_version INTEGER NOT NULL, budget_json TEXT NOT NULL CHECK(json_valid(budget_json)),
            status TEXT NOT NULL CHECK(status IN ('proposed','active','completed','held','cancelled')),
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            CHECK(incident_id IS NOT NULL OR command_id IS NOT NULL), {_RUN_FK}, {_POLICY_FK},
            FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id),
            FOREIGN KEY(facility_id,run_id,command_id) REFERENCES commands(facility_id,run_id,command_id),
            FOREIGN KEY(facility_id,run_id,trigger_followup_id) REFERENCES followups(facility_id,run_id,followup_id),
            UNIQUE(facility_id,run_id,plan_id))""",
        f"""CREATE TABLE executions (
            execution_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            plan_id TEXT, incident_id TEXT, command_id TEXT,
            tool_name TEXT NOT NULL, target_ref TEXT NOT NULL, requester_ref TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            status TEXT NOT NULL CHECK(status IN ('requested','accepted','running','succeeded','failed','held','cancelled','unknown')),
            based_on_state_version INTEGER NOT NULL CHECK(based_on_state_version>=0),
            expected_resource_version INTEGER CHECK(expected_resource_version>=1),
            policy_version INTEGER NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('synthetic_demo','simulated','live')),
            result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)), error_code TEXT,
            applied_sim_time_ms INTEGER CHECK(applied_sim_time_ms>=0), cancellation_requested_at TEXT,
            knowledge_evidence_json TEXT CHECK(knowledge_evidence_json IS NULL OR json_valid(knowledge_evidence_json)),
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            {_RUN_FK}, {_POLICY_FK},
            FOREIGN KEY(facility_id,run_id,plan_id) REFERENCES plans(facility_id,run_id,plan_id),
            FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id),
            FOREIGN KEY(facility_id,run_id,command_id) REFERENCES commands(facility_id,run_id,command_id),
            UNIQUE(facility_id,requester_ref,idempotency_key), UNIQUE(facility_id,run_id,execution_id))""",
        f"""CREATE TABLE notifications (
            notification_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            execution_id TEXT NOT NULL UNIQUE, incident_id TEXT, command_id TEXT,
            context_key TEXT NOT NULL, registered_vehicle_id TEXT,
            recipient_user_id TEXT NOT NULL REFERENCES users(user_id),
            purpose TEXT NOT NULL CHECK(purpose IN ('move_request','owner_report')),
            contact_sequence INTEGER NOT NULL CHECK(contact_sequence>=1),
            delivery_status TEXT NOT NULL CHECK(delivery_status IN ('queued','channel_accepted','client_received','failed','unknown')),
            message_template TEXT NOT NULL, message_json TEXT NOT NULL CHECK(json_valid(message_json)),
            response_due_at TEXT, mode TEXT NOT NULL CHECK(mode IN ('synthetic_demo','simulated','live')),
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            CHECK(incident_id IS NOT NULL OR command_id IS NOT NULL),
            CHECK(purpose!='move_request' OR (incident_id IS NOT NULL AND registered_vehicle_id IS NOT NULL)),
            {_RUN_FK},
            FOREIGN KEY(facility_id,run_id,execution_id) REFERENCES executions(facility_id,run_id,execution_id),
            FOREIGN KEY(facility_id,run_id,incident_id) REFERENCES incidents(facility_id,run_id,incident_id),
            FOREIGN KEY(facility_id,run_id,command_id) REFERENCES commands(facility_id,run_id,command_id),
            FOREIGN KEY(facility_id,registered_vehicle_id) REFERENCES vehicles(facility_id,registered_vehicle_id),
            UNIQUE(context_key,recipient_user_id,purpose,contact_sequence), UNIQUE(facility_id,run_id,notification_id))""",
        f"""CREATE TABLE delivery_attempts (
            attempt_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            notification_id TEXT NOT NULL, attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
            channel TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('requested','accepted','failed','unknown')),
            requested_at TEXT NOT NULL, completed_at TEXT, error_code TEXT,
            created_at {_STAMP}, updated_at {_STAMP}, resource_version INTEGER NOT NULL DEFAULT 1 CHECK(resource_version>=1),
            {_RUN_FK}, FOREIGN KEY(facility_id,run_id,notification_id) REFERENCES notifications(facility_id,run_id,notification_id),
            UNIQUE(notification_id,attempt_number))""",
        f"""CREATE TABLE notification_receipts (
            receipt_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            notification_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(user_id),
            client_request_id TEXT NOT NULL, received_at TEXT NOT NULL, recorded_at {_STAMP},
            {_RUN_FK}, FOREIGN KEY(facility_id,run_id,notification_id) REFERENCES notifications(facility_id,run_id,notification_id),
            UNIQUE(user_id,client_request_id))""",
        f"""CREATE TABLE notification_responses (
            response_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            notification_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(user_id),
            client_request_id TEXT NOT NULL,
            response TEXT NOT NULL CHECK(response IN ('acknowledged','will_move','cannot_move','question')),
            text TEXT, responded_at TEXT NOT NULL,
            {_RUN_FK}, FOREIGN KEY(facility_id,run_id,notification_id) REFERENCES notifications(facility_id,run_id,notification_id),
            UNIQUE(user_id,client_request_id))""",
        f"""CREATE TABLE outbox_events (
            event_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL, run_id TEXT NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            dispatch_status TEXT NOT NULL CHECK(dispatch_status IN ('pending','sent','failed')),
            created_at {_STAMP}, dispatched_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            stream_seq INTEGER UNIQUE REFERENCES events(seq), {_RUN_FK})""",
        f"""CREATE TABLE audit_events (
            audit_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id), run_id TEXT,
            actor_ref TEXT NOT NULL, action TEXT NOT NULL, target_ref TEXT NOT NULL,
            outcome TEXT NOT NULL, reason_code TEXT NOT NULL, occurred_at TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            FOREIGN KEY(facility_id,run_id) REFERENCES run_facilities(facility_id,run_id))""",
        """CREATE TABLE business_requests (
            facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
            requester_ref TEXT NOT NULL, key TEXT NOT NULL,
            argument_hash TEXT NOT NULL, response_json TEXT NOT NULL CHECK(json_valid(response_json)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(facility_id,requester_ref,key))""",
    ]
    for table in ("observation_evidence", "incident_evidence", "notification_receipts", "notification_responses", "audit_events"):
        for operation in ("UPDATE", "DELETE"):
            statements.append(f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Append-only business record'); END")
    statements.extend([
        "CREATE INDEX incidents_scope ON incidents(facility_id,run_id,status,updated_at)",
        "CREATE INDEX executions_pending ON executions(facility_id,status,created_at)",
        "CREATE INDEX notifications_recipient ON notifications(recipient_user_id,created_at)",
        "CREATE INDEX followups_sim_due ON followups(status,clock,due_sim_time_ms)",
        "CREATE INDEX followups_wall_due ON followups(status,clock,due_at)",
        "CREATE INDEX outbox_pending ON outbox_events(dispatch_status,event_id)",
    ])
    try:
        db.execute("BEGIN IMMEDIATE")
        for statement in statements:
            db.execute(statement)
        db.execute("PRAGMA user_version=4")
        db.commit()
    except Exception:
        db.rollback()
        raise
