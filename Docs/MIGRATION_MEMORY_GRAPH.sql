-- ============================================================
-- Chief OS — Memory & Knowledge Graph Migration
-- Run this in Supabase SQL Editor (Dashboard → SQL → New Query)
-- Prerequisites: Enable pgvector extension first
-- ============================================================

-- 0. Enable pgvector (required for embedding columns)
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- 1. MEMORIES TABLE — Semantic long-term memory layer
-- ============================================================

CREATE TABLE IF NOT EXISTS memories (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     text NOT NULL,
    content     text NOT NULL,
    memory_type text NOT NULL DEFAULT 'note',     -- note, reflection, insight
    metadata    jsonb DEFAULT '{}',
    embedding   vector(768),                       -- gemini-embedding-2-preview output
    created_at  timestamptz DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(user_id, memory_type);

-- Vector index (IVFFlat — good up to ~1M rows; switch to HNSW at scale)
-- Note: IVFFlat requires at least `lists` rows to exist before querying.
-- For cold-start safety, we use a small list count.
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- RLS
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "memories_tenant_policy" ON memories;
CREATE POLICY "memories_tenant_policy" ON memories
    USING (true) WITH CHECK (true);


-- ============================================================
-- 2. GRAPH_NODES TABLE — Knowledge graph vertices
-- ============================================================

CREATE TABLE IF NOT EXISTS graph_nodes (
    id         uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id    text NOT NULL,
    label      text NOT NULL,
    type       text NOT NULL DEFAULT 'concept',   -- person, organization, project, concept, mission, emotional_state
    metadata   jsonb DEFAULT '{}',
    created_at timestamptz DEFAULT now()
);

-- Unique per user+label (prevents duplicate "John" nodes for the same user)
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_user_label
    ON graph_nodes(user_id, label);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_user_type
    ON graph_nodes(user_id, type);

-- RLS
ALTER TABLE graph_nodes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "graph_nodes_tenant_policy" ON graph_nodes;
CREATE POLICY "graph_nodes_tenant_policy" ON graph_nodes
    USING (true) WITH CHECK (true);


-- ============================================================
-- 3. GRAPH_EDGES TABLE — Knowledge graph relationships
-- ============================================================

CREATE TABLE IF NOT EXISTS graph_edges (
    id              uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id         text NOT NULL,
    source_node_id  uuid NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_node_id  uuid NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relationship    text NOT NULL,                 -- relates_to, works_at, parent_of, etc.
    weight          float DEFAULT 1.0,
    metadata        jsonb DEFAULT '{}',
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_user   ON graph_edges(user_id);

-- RLS
ALTER TABLE graph_edges ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "graph_edges_tenant_policy" ON graph_edges;
CREATE POLICY "graph_edges_tenant_policy" ON graph_edges
    USING (true) WITH CHECK (true);


-- ============================================================
-- 4. AGENT_QUEUE TABLE — Research agent task queue
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_queue (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      text NOT NULL,
    task         text NOT NULL,
    status       text NOT NULL DEFAULT 'pending',  -- pending, processing, completed, failed
    metadata     jsonb DEFAULT '{}',
    completed_at timestamptz,
    created_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_queue_user_status ON agent_queue(user_id, status);

-- RLS
ALTER TABLE agent_queue ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "agent_queue_tenant_policy" ON agent_queue;
CREATE POLICY "agent_queue_tenant_policy" ON agent_queue
    USING (true) WITH CHECK (true);


-- ============================================================
-- 5. ALTER EXISTING TABLES — Add new columns
-- ============================================================

-- resources: add embedding + enrichment columns
ALTER TABLE resources ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE resources ADD COLUMN IF NOT EXISTS enriched_at timestamptz;
ALTER TABLE resources ADD COLUMN IF NOT EXISTS strategic_note text;

-- tasks: add duration_mins for calendar block sizing
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS duration_mins integer DEFAULT 15;


-- ============================================================
-- 6. RPC: match_memories — Vector similarity search
--    Multi-tenant safe: requires filter_user_id parameter
-- ============================================================

CREATE OR REPLACE FUNCTION match_memories(
    query_embedding vector(768),
    match_count     int   DEFAULT 5,
    match_threshold float DEFAULT 0.6,
    filter_user_id  text  DEFAULT NULL
)
RETURNS TABLE (
    id          bigint,
    content     text,
    memory_type text,
    metadata    jsonb,
    similarity  float,
    created_at  timestamptz
)
LANGUAGE plpgsql
STABLE                    -- safe for read replicas
AS $$
BEGIN
    RETURN QUERY
    SELECT
        m.id,
        m.content,
        m.memory_type,
        m.metadata,
        (1 - (m.embedding <=> query_embedding))::float AS similarity,
        m.created_at
    FROM memories m
    WHERE
        -- Mandatory tenant filter: never return cross-tenant data
        m.user_id = filter_user_id
        AND m.embedding IS NOT NULL
        AND (1 - (m.embedding <=> query_embedding)) > match_threshold
    ORDER BY m.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- ============================================================
-- 7. RPC: match_resources — Vector similarity search on resources
-- ============================================================

CREATE OR REPLACE FUNCTION match_resources(
    query_embedding vector(768),
    match_count     int   DEFAULT 5,
    match_threshold float DEFAULT 0.5,
    filter_user_id  text  DEFAULT NULL
)
RETURNS TABLE (
    id              bigint,
    url             text,
    title           text,
    summary         text,
    category        text,
    strategic_note  text,
    similarity      float,
    created_at      timestamptz
)
LANGUAGE plpgsql
STABLE
AS $$
BEGIN
    RETURN QUERY
    SELECT
        r.id,
        r.url,
        r.title,
        r.summary,
        r.category,
        r.strategic_note,
        (1 - (r.embedding <=> query_embedding))::float AS similarity,
        r.created_at
    FROM resources r
    WHERE
        r.user_id = filter_user_id
        AND r.embedding IS NOT NULL
        AND (1 - (r.embedding <=> query_embedding)) > match_threshold
    ORDER BY r.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;


-- ============================================================
-- DONE. Verify by running:
--   SELECT count(*) FROM memories;
--   SELECT count(*) FROM graph_nodes;
--   SELECT count(*) FROM graph_edges;
--   SELECT count(*) FROM agent_queue;
-- ============================================================
