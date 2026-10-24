"""P15 shared schema: interpretation cache, operator commands, steering intent (migration 0008)."""
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from conftest import ROOT, needs_db


@needs_db
def test_0008_adds_p15_tables_and_is_reversible(db_url):
    from app import db
    with db.engine().connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0008"
    insp = inspect(db.engine())
    assert {"interpretations", "operator_commands"} <= set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("operator_commands")}
    assert {"incident_id", "instruction", "status", "plan", "plan_sha256", "dry_run", "side_effects", "result",
            "verification", "rollback", "error", "actor", "message_id", "created_at", "updated_at"} <= cols
    icols = {c["name"] for c in insp.get_columns("interpretations")}
    assert {"incident_id", "item_key", "raw_sha256", "ko", "source", "agent_run_id"} <= icols
    mcols = {c["name"] for c in insp.get_columns("operator_messages")}
    assert {"intent", "intent_detail"} <= mcols

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["sqlalchemy_url"] = db_url
    command.downgrade(cfg, "0007")
    insp = inspect(db.engine())
    assert "operator_commands" not in insp.get_table_names()
    assert "intent" not in {c["name"] for c in insp.get_columns("operator_messages")}
    db.run_migrations(db_url)
    with db.engine().connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == "0008"


@needs_db
def test_interpretation_cache_is_unique_per_raw_text(db_url):
    import pytest
    from sqlalchemy.exc import IntegrityError
    from app import db
    from app.models import Incident, Interpretation
    with db.session() as s:
        inc = Incident(fingerprint="f", namespace="n", workload="w", title="t")
        s.add(inc)
        s.flush()
        iid = inc.id
        s.add(Interpretation(incident_id=iid, item_key="E5_recent_events#0", raw_sha256="a" * 64, ko="x", source="rules"))
    with pytest.raises(IntegrityError):
        with db.session() as s:
            s.add(Interpretation(incident_id=iid, item_key="E5_recent_events#0", raw_sha256="a" * 64, ko="y", source="llm"))
