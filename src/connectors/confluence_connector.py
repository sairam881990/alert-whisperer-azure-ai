"""
Confluence MCP Connector.

Connects to the Confluence MCP Server to search and retrieve runbook pages,
operational procedures, and troubleshooting guides. Provides structured
access to Confluence content for RAG indexing and real-time lookups.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import structlog

from src.connectors.mcp_base import BaseMCPClient
from src.models import DocumentChunk

logger = structlog.get_logger(__name__)


class ConfluenceMCPClient(BaseMCPClient):
    """
    MCP client for Confluence.

    Provides methods to:
    - Search runbooks by keyword or CQL
    - Retrieve page content by ID or title
    - List pages in a space
    - Extract structured steps from runbook pages
    - Index content for the RAG vector store
    """

    def __init__(
        self,
        server_url: str,
        base_url: str = "",
        space_keys: Optional[list[str]] = None,
        **kwargs: Any,
    ):
        super().__init__(server_url=server_url, **kwargs)
        self.base_url = base_url
        self.space_keys = space_keys or ["RUNBOOKS"]

    async def validate_connection(self) -> bool:
        """Validate by listing spaces."""
        try:
            result = await self.call_tool(
                tool_name="search",
                arguments={"query": "type=space", "limit": 1},
            )
            return result is not None
        except Exception as e:
            logger.error("confluence_validation_failed", error=str(e))
            return False

    async def search_pages(
        self,
        query: str,
        space_keys: Optional[list[str]] = None,
        limit: int = 10,
        max_pages: int = 5,
        page_size: int = 25,
    ) -> list[dict[str, Any]]:
        """
        M1: Search Confluence pages with CQL pagination.

        Auto-fetches subsequent pages using `start` + `limit` parameters
        until `limit` total results are collected or max_pages is reached.
        Handles the `_links.next` pattern from the Confluence REST API.

        Args:
            query: Search text or CQL query
            space_keys: Confluence spaces to search (defaults to configured spaces)
            limit: Total maximum results to return
            max_pages: Maximum number of API pages to fetch (default 5)
            page_size: Number of results per API call (default 25)

        Returns:
            List of page summaries with id, title, space, and excerpt
        """
        spaces = space_keys or self.space_keys
        space_filter = " OR ".join([f'space="{sk}"' for sk in spaces])
        cql = f'({space_filter}) AND text ~ "{query}"'

        logger.info("confluence_search", cql=cql[:200], limit=limit, max_pages=max_pages)

        all_results: list[dict[str, Any]] = []
        start = 0
        # Use the smaller of page_size and limit as our per-call batch
        batch_size = min(page_size, limit)

        for page_num in range(max_pages):
            result = await self.call_tool(
                tool_name="search",
                arguments={
                    "query": cql,
                    "limit": batch_size,
                    "start": start,
                },
            )

            page_results = self._parse_search_results(result)

            if not page_results:
                logger.debug(
                    "confluence_search_exhausted",
                    page_num=page_num,
                    total_so_far=len(all_results),
                )
                break

            all_results.extend(page_results)

            logger.debug(
                "confluence_search_page",
                page_num=page_num,
                page_results=len(page_results),
                total_so_far=len(all_results),
            )

            if len(all_results) >= limit:
                break

            # Check for _links.next continuation (Confluence REST API pattern)
            has_next = False
            if isinstance(result, dict):
                links = result.get("_links", {})
                if isinstance(links, dict) and links.get("next"):
                    has_next = True
                # Also check if the raw result carries a size hint
                result_size = result.get("size", 0)
                if result_size < batch_size:
                    has_next = False  # Last page
            elif isinstance(result, list) and len(page_results) < batch_size:
                # List response shorter than requested — no more pages
                has_next = False
            else:
                has_next = len(page_results) >= batch_size

            if not has_next:
                break

            start += batch_size
            # Adjust batch_size for the final partial page if needed
            remaining = limit - len(all_results)
            if remaining <= 0:
                break
            batch_size = min(page_size, remaining)

        return all_results[:limit]

    async def get_page_content(
        self,
        page_id: str,
        expand: str = "body.storage,version,ancestors",
    ) -> dict[str, Any]:
        """
        Retrieve full page content by page ID.

        Args:
            page_id: Confluence page ID
            expand: Fields to expand in the response

        Returns:
            Page content with title, body, and metadata
        """
        logger.info("confluence_get_page", page_id=page_id)

        result = await self.call_tool(
            tool_name="get_page",
            arguments={"page_id": page_id, "expand": expand},
        )

        return self._parse_page_content(result)

    async def get_page_by_title(
        self,
        title: str,
        space_key: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        Find and retrieve a page by its title.

        Args:
            title: Page title to search for
            space_key: Confluence space key

        Returns:
            Page content or None if not found
        """
        space = space_key or self.space_keys[0]
        results = await self.search_pages(
            query=f'title="{title}"',
            space_keys=[space],
            limit=1,
        )

        if results:
            return await self.get_page_content(results[0]["id"])
        return None

    async def get_runbook(
        self,
        confluence_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Retrieve a runbook page and extract structured steps.

        Args:
            confluence_id: Confluence page ID (e.g., from pipeline_ownership.json)

        Returns:
            Structured runbook with title, steps, and metadata
        """
        page = await self.get_page_content(confluence_id)
        if not page:
            return None

        # Extract structured steps from the page body
        steps = self._extract_runbook_steps(page.get("body", ""))

        return {
            "page_id": confluence_id,
            "title": page.get("title", ""),
            "steps": steps,
            "raw_content": page.get("body", ""),
            "url": f"{self.base_url}/pages/viewpage.action?pageId={confluence_id}",
            "last_updated": page.get("version", {}).get("when", ""),
        }

    async def search_runbooks(
        self,
        error_context: str,
        pipeline_name: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Search for relevant runbooks based on error context.
        Combines keyword search with pipeline name for targeted results.

        Args:
            error_context: Error message or description to search for
            pipeline_name: Pipeline name to narrow results
            limit: Maximum runbooks to return

        Returns:
            List of matching runbook summaries
        """
        # Build a focused search query
        keywords = self._extract_keywords(error_context)
        search_terms = " ".join(keywords[:5])

        if pipeline_name:
            search_terms = f"{pipeline_name} {search_terms}"

        results = await self.search_pages(
            query=search_terms,
            limit=limit,
        )

        # Enrich results with runbook-specific data
        enriched = []
        for r in results:
            try:
                page = await self.get_page_content(r["id"])
                if page:
                    steps = self._extract_runbook_steps(page.get("body", ""))
                    enriched.append({
                        **r,
                        "steps": steps,
                        "step_count": len(steps),
                        "url": f"{self.base_url}/pages/viewpage.action?pageId={r['id']}",
                    })
            except Exception as e:
                logger.warning("runbook_enrichment_failed", page_id=r["id"], error=str(e))
                enriched.append(r)

        return enriched

    async def get_pages_for_indexing(
        self,
        space_keys: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[DocumentChunk]:
        """
        Retrieve pages from Confluence for RAG vector store indexing.

        Args:
            space_keys: Spaces to index
            limit: Maximum pages to retrieve

        Returns:
            List of DocumentChunk objects ready for embedding
        """
        spaces = space_keys or self.space_keys
        chunks: list[DocumentChunk] = []

        for space in spaces:
            try:
                results = await self.search_pages(
                    query=f'type="page"',
                    space_keys=[space],
                    limit=limit,
                )

                for page_summary in results:
                    page = await self.get_page_content(page_summary["id"])
                    if page and page.get("body"):
                        # Create document chunks from page content
                        page_chunks = self._create_chunks(
                            page_id=page_summary["id"],
                            title=page.get("title", ""),
                            content=page.get("body", ""),
                            space=space,
                        )
                        chunks.extend(page_chunks)

            except Exception as e:
                logger.error("confluence_indexing_error", space=space, error=str(e))

        logger.info("confluence_indexed", total_chunks=len(chunks))
        return chunks

    # ─── M4: Per-Connector Health Check ───────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        M4: Confluence-specific health check.
        Pings the MCP /health endpoint and then lists a Confluence space
        to verify the underlying Confluence instance is reachable.
        """
        status = await super().health_check()
        status["connector"] = "confluence"
        status["base_url"] = self.base_url
        status["space_keys"] = self.space_keys

        if status["healthy"]:
            try:
                result = await self.call_tool(
                    tool_name="search",
                    arguments={"query": "type=space", "limit": 1, "start": 0},
                )
                status["confluence_reachable"] = result is not None
                status["confluence_detail"] = "Space listing succeeded"
            except Exception as exc:
                status["confluence_reachable"] = False
                status["confluence_detail"] = f"{type(exc).__name__}: {exc}"

        return status

    # ─── M9: GraphRAG — Page Link Graph ──────────────────

    async def get_linked_pages(
        self,
        page_id: str,
        depth: int = 1,
    ) -> dict[str, Any]:
        """
        M9: Return the page link graph for a given Confluence page.
        Useful for GraphRAG dependency mapping across runbook / operational
        knowledge base pages.

        Args:
            page_id: Root page ID to start from
            depth: How many levels of links to traverse (1 = direct links only)

        Returns:
            Dict with:
            - root_page_id (str)
            - links: list of {page_id, title, url, link_type}
            - children: list of child page summaries (depth=1)
        """
        logger.info("confluence_get_linked_pages", page_id=page_id, depth=depth)

        link_graph: dict[str, Any] = {
            "root_page_id": page_id,
            "links": [],
            "children": [],
        }

        # Fetch direct children of this page
        try:
            children_result = await self.call_tool(
                tool_name="get_child_pages",
                arguments={"page_id": page_id, "limit": 50},
            )
            if isinstance(children_result, list):
                link_graph["children"] = [
                    {
                        "page_id": str(item.get("id", "")),
                        "title": item.get("title", ""),
                        "url": (
                            item.get("_links", {}).get("webui", "")
                            if isinstance(item.get("_links"), dict) else ""
                        ),
                        "link_type": "child",
                    }
                    for item in children_result
                    if isinstance(item, dict)
                ]
        except Exception as exc:
            logger.warning("confluence_children_fetch_failed", page_id=page_id, error=str(exc))
            link_graph["children_error"] = str(exc)

        # Fetch inline links from the page body (storage format)
        try:
            page = await self.get_page_content(page_id, expand="body.storage")
            body = page.get("body", "")
            if body:
                import re as _re
                # Extract Confluence page links: /pages/viewpage.action?pageId=XXXXX
                page_id_refs = _re.findall(r'pageId=(\d+)', body)
                # Also match wiki-style links: <ri:page ri:content-title="...">
                title_refs = _re.findall(r'ri:content-title="([^"]+)"', body)

                for ref_id in set(page_id_refs):
                    if ref_id != page_id:
                        link_graph["links"].append({
                            "page_id": ref_id,
                            "title": "",  # Title not available without extra fetch
                            "url": f"{self.base_url}/pages/viewpage.action?pageId={ref_id}",
                            "link_type": "inline_page_id",
                        })
                for title in set(title_refs):
                    link_graph["links"].append({
                        "page_id": "",
                        "title": title,
                        "url": "",
                        "link_type": "inline_title_ref",
                    })
        except Exception as exc:
            logger.warning("confluence_link_extract_failed", page_id=page_id, error=str(exc))
            link_graph["links_error"] = str(exc)

        return link_graph

    # ─── Internal Helpers ─────────────────────────

    def _parse_search_results(self, raw: Any) -> list[dict[str, Any]]:
        """Parse MCP search results into structured page summaries."""
        if not raw:
            return []

        if isinstance(raw, str):
            # Text response - attempt to extract page references
            pages = []
            for line in raw.split("\n"):
                if "id:" in line.lower() or "title:" in line.lower():
                    pages.append({"raw": line.strip()})
            return pages

        if isinstance(raw, list):
            return [
                {
                    "id": str(item.get("id", "")),
                    "title": item.get("title", ""),
                    "space": item.get("space", {}).get("key", "") if isinstance(item.get("space"), dict) else "",
                    "excerpt": item.get("excerpt", "")[:300],
                    "url": item.get("_links", {}).get("webui", "") if isinstance(item.get("_links"), dict) else "",
                }
                for item in raw
                if isinstance(item, dict)
            ]

        return []

    def _parse_page_content(self, raw: Any) -> dict[str, Any]:
        """Parse MCP page content response."""
        if not raw:
            return {}

        if isinstance(raw, dict):
            body = raw.get("body", {})
            if isinstance(body, dict):
                body_content = body.get("storage", {}).get("value", "")
            else:
                body_content = str(body)

            return {
                "id": raw.get("id", ""),
                "title": raw.get("title", ""),
                "body": self._clean_html(body_content),
                "version": raw.get("version", {}),
                "space": raw.get("space", {}),
            }

        if isinstance(raw, str):
            return {"body": raw, "title": "", "id": ""}

        return {}

    @staticmethod
    def _extract_runbook_steps(content: str) -> list[str]:
        """
        Extract structured steps from runbook content.
        Handles numbered lists, bullet points, and heading-based steps.
        """
        steps = []

        # Pattern 1: Numbered steps (1. Step text, Step 1: text)
        numbered = re.findall(r'(?:^|\n)\s*(?:Step\s*)?\d+[.:)]\s*(.+?)(?=\n|$)', content, re.IGNORECASE)
        if numbered:
            steps.extend([s.strip() for s in numbered if len(s.strip()) > 10])

        # Pattern 2: Bullet points with action verbs
        action_verbs = r'(?:Check|Verify|Run|Execute|Open|Navigate|Click|Restart|Scale|Monitor|Update|Deploy|Rollback)'
        bullets = re.findall(rf'(?:^|\n)\s*[-•*]\s*({action_verbs}.+?)(?=\n|$)', content, re.IGNORECASE)
        if bullets:
            steps.extend([s.strip() for s in bullets if len(s.strip()) > 10])

        # Pattern 3: Heading-based sections
        if not steps:
            headings = re.findall(r'(?:^|\n)#+\s*(.+?)(?=\n)', content)
            steps.extend([h.strip() for h in headings if len(h.strip()) > 5])

        return steps if steps else ["No structured steps found in runbook."]

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract meaningful keywords from error text for search."""
        # Remove common noise words
        stop_words = {
            "the", "a", "an", "is", "was", "were", "are", "been", "be",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "shall", "can", "to", "of",
            "in", "for", "on", "with", "at", "by", "from", "as", "into",
            "through", "during", "before", "after", "above", "below",
            "between", "out", "off", "over", "under", "again", "further",
            "then", "once", "that", "this", "these", "those", "it", "its",
            "error", "exception", "failed", "failure",
        }

        # Tokenize and filter
        words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', text)
        keywords = [
            w for w in words
            if w.lower() not in stop_words and len(w) > 2
        ]

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for kw in keywords:
            if kw.lower() not in seen:
                seen.add(kw.lower())
                unique.append(kw)

        return unique

    @staticmethod
    def _clean_html(html: str) -> str:
        """Strip HTML tags and normalize whitespace."""
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _create_chunks(
        self,
        page_id: str,
        title: str,
        content: str,
        space: str,
        chunk_size: int = 1000,
        overlap: int = 200,
    ) -> list[DocumentChunk]:
        """Split page content into overlapping chunks for embedding."""
        chunks = []
        clean_content = self._clean_html(content)

        if not clean_content:
            return chunks

        # Split on paragraph boundaries when possible
        paragraphs = clean_content.split("\n\n")
        current_chunk = ""
        chunk_index = 0

        for para in paragraphs:
            if len(current_chunk) + len(para) > chunk_size and current_chunk:
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"confluence_{page_id}_{chunk_index}",
                        source_type="confluence",
                        source_id=page_id,
                        source_title=title,
                        content=current_chunk.strip(),
                        metadata={
                            "space": space,
                            "chunk_index": chunk_index,
                            "url": f"{self.base_url}/pages/viewpage.action?pageId={page_id}",
                        },
                    )
                )
                # Keep overlap from previous chunk
                current_chunk = current_chunk[-overlap:] + " " + para
                chunk_index += 1
            else:
                current_chunk += "\n\n" + para if current_chunk else para

        # Don't forget the last chunk
        if current_chunk.strip():
            chunks.append(
                DocumentChunk(
                    chunk_id=f"confluence_{page_id}_{chunk_index}",
                    source_type="confluence",
                    source_id=page_id,
                    source_title=title,
                    content=current_chunk.strip(),
                    metadata={
                        "space": space,
                        "chunk_index": chunk_index,
                        "url": f"{self.base_url}/pages/viewpage.action?pageId={page_id}",
                    },
                )
            )

        return chunks
