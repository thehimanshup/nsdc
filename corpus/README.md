# Corpus (ST-201)
- `SOURCE_REGISTER.csv` — every document's provenance. Rows marked TO_DOWNLOAD
  need the DATA owner to fetch the PDF/HTML from the listed public source and
  drop it in `raw/<doc_id>.<ext>`, then set status=DOWNLOADED and fill
  version + last_updated from the document itself.
- Golden-path QPs (deep-linked in KG + gold set): HSS/Q5101, HSS/Q5102, HSS/Q0301.
- Rule: nothing enters `raw/` without a register row. Ingestion (ST-203)
  reads the register as the source of DocumentMeta and FAILS CLOSED on
  missing fields (see backend/substrate/schemas.py).
- centre-registry is SYNTHETIC — clearly label as sample data in any demo.
