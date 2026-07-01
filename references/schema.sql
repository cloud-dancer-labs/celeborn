-- Celeborn derived search index.
-- This database is DERIVED from the markdown in .context/ and is never authoritative.
-- It is gitignored and fully regenerable: `celeborn index` drops and rebuilds it from scratch.
--
-- Design: a single FTS5 table holds searchable text (title/body/tags) plus unindexed
-- metadata columns. There is no separate "content" table — the markdown files are the content.
-- A links table captures the [[wikilink]] graph between notes. A meta table records build state.

DROP TABLE IF EXISTS memory_fts;
DROP TABLE IF EXISTS links;
DROP TABLE IF EXISTS meta;

-- detail defaults to 'full' — required by snippet(); do not set detail='none'.
-- remove_diacritics 2 makes "café" match "cafe".
CREATE VIRTUAL TABLE memory_fts USING fts5(
    title,
    body,
    tags,
    tier UNINDEXED,
    source_file UNINDEXED,
    anchor UNINDEXED,
    updated_at UNINDEXED,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TABLE links (
    src_file   TEXT NOT NULL,
    src_anchor TEXT,
    target     TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
