"""Long-term memory — persistent, cross-session document store.

Each stored chunk carries the three pathways (summary · full-text/BM25 · embedding)
so a later, related session can recall it. `search()` returns one ranked id-list
PER pathway; `recall.py` fuses them with RRF and the router synthesizes a briefing.

Two implementations behind one interface:
  * `InMemoryLongTermStore` — reuses the episodic BM25 + embedder; the tested
    reference and the fallback when Postgres isn't configured.
  * `PgVectorLongTermStore` — Postgres/pgvector (see db/init.sql). The same
    interface, so the recall pipeline is backend-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from forge.memory.episodic import BM25Index, HashingEmbedder, cosine, tokenize


@dataclass
class Document:
    session_id: str
    doc_id: str
    content: str
    summary: str = ""
    kind: str = "trace"
    agent: str = ""
    task_id: str = ""
    project: str = ""
    embedding: Optional[list] = None  # raw-chunk embedding (filled by the store)

    def cite(self) -> str:
        return f"{self.session_id}:{self.doc_id}"


class LongTermStore(ABC):
    @abstractmethod
    def add_document(self, doc: Document) -> None: ...

    @abstractmethod
    def search(self, query: str, query_embedding: Optional[list] = None,
               per_pathway: int = 10, project: Optional[str] = None,
               exclude_session: Optional[str] = None) -> dict:
        """Return {"summary": [doc_id...], "fulltext": [...], "vector": [...]}."""

    @abstractmethod
    def get(self, session_id: str, doc_id: str,
            project: Optional[str] = None) -> Optional[Document]: ...


class InMemoryLongTermStore(LongTermStore):
    def __init__(self, embedder: Optional[HashingEmbedder] = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self._docs: list[Document] = []
        self._summary_vecs: list[list] = []
        self._bm25 = BM25Index()

    def add_document(self, doc: Document) -> None:
        if doc.embedding is None:
            doc.embedding = self.embedder.embed(doc.content)
        self._docs.append(doc)
        self._summary_vecs.append(self.embedder.embed(doc.summary or doc.content))
        self._bm25.add(tokenize(doc.content + " " + (doc.summary or "")))

    def _eligible(self, i: int, project, exclude_session) -> bool:
        d = self._docs[i]
        if project is not None and d.project != project:
            return False
        if exclude_session is not None and d.session_id == exclude_session:
            return False
        return True

    def search(self, query, query_embedding=None, per_pathway=10,
               project=None, exclude_session=None) -> dict:
        if not self._docs:
            return {"summary": [], "fulltext": [], "vector": []}
        qv = query_embedding or self.embedder.embed(query)
        idxs = [i for i in range(len(self._docs)) if self._eligible(i, project, exclude_session)]

        def ranked(scorer) -> list:
            scored = [(scorer(i), self._docs[i].doc_id) for i in idxs]
            scored = [s for s in scored if s[0] > 0.0]
            scored.sort(key=lambda s: s[0], reverse=True)
            return [doc_id for _s, doc_id in scored[:per_pathway]]

        return {
            "summary": ranked(lambda i: cosine(qv, self._summary_vecs[i])),
            "fulltext": ranked(lambda i: self._bm25.score(query, i)),
            "vector": ranked(lambda i: cosine(qv, self._docs[i].embedding)),
        }

    def get(self, session_id, doc_id, project=None) -> Optional[Document]:
        # doc_id is unique only within a project, so scope by project when given;
        # prefer an exact session match, else any doc with that id in scope.
        scoped = [d for d in self._docs
                  if d.doc_id == doc_id and (project is None or d.project == project)]
        if not scoped:
            return None
        return next((d for d in scoped if d.session_id == session_id), scoped[0])


class PgVectorLongTermStore(LongTermStore):
    """Postgres/pgvector backend (schema in db/init.sql).

    `conn` is a live `psycopg` connection; `embedder` fills the vector column.
    Imports of psycopg/pgvector are the caller's responsibility so the package
    stays importable without them.
    """

    def __init__(self, conn, embedder) -> None:
        self.conn = conn
        self.embedder = embedder
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Self-heal the uniqueness key on already-running docker volumes.

        The original schema keyed documents on (session_id, doc_id) — but session
        ids (S1, S2, …) are only unique WITHIN a project, so two projects' `S1-T1`
        would collide and ON CONFLICT would silently overwrite the wrong project's
        row. The correct key is (project, session_id, doc_id). db/init.sql carries
        this for fresh volumes; here we migrate existing ones idempotently."""

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE documents DROP CONSTRAINT IF EXISTS "
                    "documents_session_id_doc_id_key"
                )
                cur.execute(
                    "SELECT 1 FROM pg_constraint WHERE conname = "
                    "'documents_project_session_doc_key'"
                )
                if cur.fetchone() is None:
                    cur.execute(
                        "ALTER TABLE documents ADD CONSTRAINT "
                        "documents_project_session_doc_key "
                        "UNIQUE (project, session_id, doc_id)"
                    )
        except Exception:
            pass  # best-effort; a fresh volume already has the right key

    def add_document(self, doc: Document) -> None:
        emb = doc.embedding or self.embedder.embed(doc.content)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                  (session_id, doc_id, agent, task_id, kind, project,
                   content, summary, embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (project, session_id, doc_id) DO UPDATE SET
                  content=EXCLUDED.content, summary=EXCLUDED.summary,
                  embedding=EXCLUDED.embedding
                """,
                (doc.session_id, doc.doc_id, doc.agent, doc.task_id, doc.kind,
                 doc.project, doc.content, doc.summary, emb),
            )
        self.conn.commit()

    def search(self, query, query_embedding=None, per_pathway=10,
               project=None, exclude_session=None) -> dict:
        qv = query_embedding or self.embedder.embed(query)
        where = []
        params_tail: list = []
        if project is not None:
            where.append("project = %s")
            params_tail.append(project)
        if exclude_session is not None:
            where.append("session_id <> %s")
            params_tail.append(exclude_session)
        # Scope filters live in the WHERE clause, BEFORE ORDER BY — so params are
        # ordered (match params, scope params, order params, LIMIT).
        clause = (" AND " + " AND ".join(where)) if where else ""

        def run(where_sql: str, where_params: tuple,
                order_sql: str, order_params: tuple) -> list:
            sql = (f"SELECT doc_id FROM documents WHERE {where_sql}{clause} "
                   f"ORDER BY {order_sql} LIMIT %s")
            params = (*where_params, *params_tail, *order_params, per_pathway)
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return [r[0] for r in cur.fetchall()]

        # Lexical pathways use OR semantics: `plainto_tsquery` ANDs every lexeme,
        # so a task title rarely matches a prior card (which described a different
        # feature) and the lexical pathways return nothing — only vector ever fired.
        # Rewriting the normalized query's `&` to `|` makes it match ANY shared
        # term, ranked by ts_rank_cd — the intended BM25-ish behavior.
        OR_Q = "replace(plainto_tsquery('english', %s)::text, '&', '|')::tsquery"

        # Three pathways, three rankings, all returning doc_id.
        summary = run(
            f"to_tsvector('english', coalesce(summary,'')) @@ {OR_Q}",
            (query,),
            f"ts_rank_cd(to_tsvector('english', coalesce(summary,'')), {OR_Q}) DESC",
            (query,),
        )
        fulltext = run(
            f"tsv @@ {OR_Q}", (query,),
            f"ts_rank_cd(tsv, {OR_Q}) DESC", (query,),
        )
        vector = run(
            "embedding IS NOT NULL", (),
            "embedding <=> %s::vector ASC", (qv,),
        )
        return {"summary": summary, "fulltext": fulltext, "vector": vector}

    def get(self, session_id, doc_id, project=None) -> Optional[Document]:
        # doc_id is unique only within a project — scope by project when given so
        # recall never resolves another project's same-named doc.
        clause = " AND project=%s" if project is not None else ""
        params = (doc_id, *( (project,) if project is not None else () ), session_id)
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, doc_id, content, summary, kind, agent, task_id, "
                "project FROM documents WHERE doc_id=%s" + clause +
                " ORDER BY (session_id=%s) DESC LIMIT 1",
                params,
            )
            row = cur.fetchone()
        if not row:
            return None
        return Document(session_id=row[0], doc_id=row[1], content=row[2],
                        summary=row[3], kind=row[4], agent=row[5], task_id=row[6],
                        project=row[7])
