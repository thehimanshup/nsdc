"""KG loader — loads curated CSVs into Neo4j and stamps a versioned,
hash-verified release (ST-302/ST-303, RFP 4.B.1a).

Curated input layout (backend/substrate/kg/curated/):
    nodes_qp.csv          qp_code,title,nsqf_level,version,source_doc_id
    nodes_nos.csv         nos_code,title,qp_code,source_doc_id
    nodes_skill.csv       id,label,nos_code
    nodes_jobrole.csv     id,title,qp_code,esco_id,onet_id
    nodes_course.csv      id,title,covers_qp,duration_hours,mode,source_doc_id
    nodes_centre.csv      id,name,district,state,offers_course
    nodes_scheme.csv      id,name,supports_course
    rules_eligibility.csv id,scheme_id,criterion,op,value,label
    xwalk_external.csv    id,scheme,label            (ESCO/O*NET occupations)

Every load produces data/kg_releases/<tag>.json with a content hash so
the Console registry (ST-1104) can verify KG integrity (RFP: "versioned
graph artefact; cryptographic integrity").

Requires: pip install neo4j
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("substrate.kg.loader")

CURATED = Path(__file__).parent / "curated"


def _rows(name: str) -> list[dict]:
    p = CURATED / name
    if not p.exists():
        log.warning("curated file missing (skipped): %s", name)
        return []
    with p.open(encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]


def content_hash() -> str:
    """Hash all curated CSVs (sorted, normalised) — the KG release hash."""
    h = hashlib.sha256()
    for p in sorted(CURATED.glob("*.csv")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


CYPHER_BATCHES = [
    ("nodes_qp.csv", """
        UNWIND $rows AS r
        MERGE (q:QualificationPack {qp_code: r.qp_code})
        SET q.title = r.title, q.version = r.version,
            q.source_doc_id = r.source_doc_id, q.valid_from = date()
        WITH q, toInteger(r.nsqf_level) AS lvl
        MATCH (n:NSQFLevel {level: lvl})
        MERGE (q)-[:NSQF_LEVEL]->(n)"""),
    ("nodes_nos.csv", """
        UNWIND $rows AS r
        MERGE (n:NOS {nos_code: r.nos_code})
        SET n.title = r.title, n.source_doc_id = r.source_doc_id
        WITH n, r MATCH (q:QualificationPack {qp_code: r.qp_code})
        MERGE (q)-[:HAS_NOS]->(n)"""),
    ("nodes_skill.csv", """
        UNWIND $rows AS r
        MERGE (s:Skill {id: r.id}) SET s.label = r.label
        WITH s, r MATCH (n:NOS {nos_code: r.nos_code})
        MERGE (n)-[:REQUIRES]->(s)"""),
    ("nodes_jobrole.csv", """
        UNWIND $rows AS r
        MERGE (j:JobRole {id: r.id}) SET j.title = r.title
        WITH j, r
        MATCH (q:QualificationPack {qp_code: r.qp_code})
        MERGE (j)-[:MAPS_TO]->(q)
        WITH j, r WHERE r.esco_id <> ''
        MERGE (e:ExternalOccupation {id: r.esco_id})
          ON CREATE SET e.scheme = 'ESCO'
        MERGE (j)-[:XWALK {scheme:'ESCO'}]->(e)"""),
    ("nodes_course.csv", """
        UNWIND $rows AS r
        MERGE (c:Course {id: r.id})
        SET c.title = r.title, c.duration_hours = toInteger(r.duration_hours),
            c.mode = r.mode, c.source_doc_id = r.source_doc_id
        WITH c, r MATCH (q:QualificationPack {qp_code: r.covers_qp})
        MERGE (c)-[:COVERS]->(q)"""),
    ("nodes_centre.csv", """
        UNWIND $rows AS r
        MERGE (t:TrainingCentre {id: r.id})
        SET t.name = r.name, t.district = r.district, t.state = r.state
        WITH t, r MATCH (c:Course {id: r.offers_course})
        MERGE (t)-[:OFFERS]->(c)"""),
    ("nodes_scheme.csv", """
        UNWIND $rows AS r
        MERGE (s:Scheme {id: r.id}) SET s.name = r.name
        WITH s, r MATCH (c:Course {id: r.supports_course})
        MERGE (s)-[:SUPPORTS]->(c)"""),
    ("rules_eligibility.csv", """
        UNWIND $rows AS r
        MERGE (e:EligibilityRule {id: r.id})
        SET e.criterion = r.criterion, e.op = r.op, e.value = r.value, e.label = r.label
        WITH e, r MATCH (s:Scheme {id: r.scheme_id})
        MERGE (s)-[:HAS_RULE]->(e)"""),
]

# The demo pathway query (ST-305 / success criterion in PRD FR-3):
PATHWAY_QUERY = """
MATCH path = (j:JobRole)-[:MAPS_TO]->(q:QualificationPack)-[:HAS_NOS]->(n:NOS)-[:REQUIRES]->(s:Skill)
WHERE toLower(j.title) CONTAINS toLower($goal)
OPTIONAL MATCH (c:Course)-[:COVERS]->(q)
OPTIONAL MATCH (sch:Scheme)-[:SUPPORTS]->(c)
OPTIONAL MATCH (q)-[:NSQF_LEVEL]->(lvl:NSQFLevel)
RETURN j.title AS job_role, q.qp_code AS qp, q.title AS qp_title,
       lvl.level AS nsqf_level,
       collect(DISTINCT n.nos_code)[..12] AS nos_codes,
       collect(DISTINCT s.label)[..12] AS skills,
       collect(DISTINCT c.title) AS courses,
       collect(DISTINCT sch.name) AS schemes
LIMIT 5
"""


def load(uri: str = "bolt://localhost:7687", user: str = "neo4j",
         password: str = "substrate-dev-pass",
         data_dir: str | Path = "data") -> dict:
    """Run bootstrap + all batches, stamp a release. Returns release info."""
    from neo4j import GraphDatabase  # lazy import — optional dep

    driver = GraphDatabase.driver(uri, auth=(user, password))
    stats: dict[str, int] = {}
    with driver.session() as session:
        bootstrap = (Path(__file__).parent / "bootstrap.cypher").read_text(encoding="utf-8")
        for stmt in [s.strip() for s in bootstrap.split(";") if s.strip()
                     and not s.strip().startswith("//")]:
            session.run(stmt)
        for csv_name, cypher in CYPHER_BATCHES:
            rows = _rows(csv_name)
            if rows:
                session.run(cypher, rows=rows)
            stats[csv_name] = len(rows)
    driver.close()

    release = {
        "tag": "kg-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M"),
        "content_hash": content_hash(),
        "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_counts": stats,
        "ontology_version": "0.1",
    }
    out = Path(data_dir) / "kg_releases"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{release['tag']}.json").write_text(json.dumps(release, indent=1))
    (out / "CURRENT").write_text(release["tag"])
    log.info("KG release %s loaded: %s", release["tag"], stats)
    return release


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(load(), indent=1))
