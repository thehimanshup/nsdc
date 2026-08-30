// Knowledge Graph v0.1 bootstrap — ontology constraints & indexes (ST-301)
// PRD §8.2 · RFP 4.B.1(a)/(g)
// Apply:  cat bootstrap.cypher | cypher-shell -u neo4j -p substrate-dev-pass

// ---- Node key constraints (13 node types) ----
CREATE CONSTRAINT sector_id IF NOT EXISTS FOR (n:Sector) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT ssc_id IF NOT EXISTS FOR (n:SSC) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT qp_code IF NOT EXISTS FOR (n:QualificationPack) REQUIRE n.qp_code IS UNIQUE;
CREATE CONSTRAINT nos_code IF NOT EXISTS FOR (n:NOS) REQUIRE n.nos_code IS UNIQUE;
CREATE CONSTRAINT jobrole_id IF NOT EXISTS FOR (n:JobRole) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT skill_id IF NOT EXISTS FOR (n:Skill) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT competency_id IF NOT EXISTS FOR (n:Competency) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT course_id IF NOT EXISTS FOR (n:Course) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT tp_id IF NOT EXISTS FOR (n:TrainingProvider) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT tc_id IF NOT EXISTS FOR (n:TrainingCentre) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT scheme_id IF NOT EXISTS FOR (n:Scheme) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (n:EligibilityRule) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT item_id IF NOT EXISTS FOR (n:AssessmentItem) REQUIRE n.id IS UNIQUE;

// ---- Crosswalk reference nodes (super-taxonomy sample, ST-304) ----
CREATE CONSTRAINT nsqf_level IF NOT EXISTS FOR (n:NSQFLevel) REQUIRE n.level IS UNIQUE;
CREATE CONSTRAINT bloom_level IF NOT EXISTS FOR (n:BloomLevel) REQUIRE n.level IS UNIQUE;
CREATE CONSTRAINT ext_occ IF NOT EXISTS FOR (n:ExternalOccupation) REQUIRE n.id IS UNIQUE;

// ---- Search indexes ----
CREATE INDEX qp_title IF NOT EXISTS FOR (n:QualificationPack) ON (n.title);
CREATE INDEX course_title IF NOT EXISTS FOR (n:Course) ON (n.title);
CREATE INDEX tc_district IF NOT EXISTS FOR (n:TrainingCentre) ON (n.district);
CREATE INDEX jobrole_title IF NOT EXISTS FOR (n:JobRole) ON (n.title);

// ---- Seed static reference nodes ----
UNWIND range(1,10) AS l
MERGE (n:NSQFLevel {level: l})
  ON CREATE SET n.label = 'NSQF Level ' + toString(l), n.version = '0.1';

UNWIND [
  {level: 1, label: 'Remember'}, {level: 2, label: 'Understand'},
  {level: 3, label: 'Apply'},    {level: 4, label: 'Analyze'},
  {level: 5, label: 'Evaluate'}, {level: 6, label: 'Create'}
] AS b
MERGE (n:BloomLevel {level: b.level})
  ON CREATE SET n.label = b.label, n.taxonomy = 'revised_bloom', n.version = '0.1';

// Relationship types (documentation — created by loaders):
//   (:QualificationPack)-[:HAS_NOS]->(:NOS)
//   (:NOS)-[:REQUIRES]->(:Skill)
//   (:Skill)-[:AT_LEVEL]->(:Competency)
//   (:JobRole)-[:MAPS_TO]->(:QualificationPack)
//   (:Course)-[:COVERS]->(:QualificationPack|:NOS)
//   (:Scheme)-[:SUPPORTS]->(:Course|:JobRole)
//   (:Scheme)-[:HAS_RULE]->(:EligibilityRule)
//   (:TrainingCentre)-[:OFFERS]->(:Course)
//   (:AssessmentItem)-[:MEASURES]->(:Skill|:NOS)
//   (:QualificationPack)-[:NSQF_LEVEL]->(:NSQFLevel)
//   (:Competency)-[:BLOOM]->(:BloomLevel)
//   (:JobRole)-[:XWALK {scheme:'ESCO'|'ONET'}]->(:ExternalOccupation)
