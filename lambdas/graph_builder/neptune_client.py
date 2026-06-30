"""
neptune_client.py — graph_builder Lambda

Thin wrapper around the Apache TinkerPop Gremlin Python client for
Amazon Neptune.  Provides idempotent upsert operations for vertices
and edges using the ``coalesce`` / ``fold`` / ``unfold`` pattern that
Neptune recommends for concurrent writes.

All public methods raise ``GremlinClientError`` (a subclass of
``RuntimeError``) on failure so callers can handle graph errors
distinctly from other exception types.

Gremlin traversal patterns used
---------------------------------
Vertex upsert (coalesce)::

    g.V()
     .has('<label>', '<key>', '<value>')
     .fold()
     .coalesce(
         __.unfold(),
         __.addV('<label>').property('<key>', '<value>')
     )
     .property('<prop_k>', '<prop_v>')
     ...
     .id()
     .next()

Edge upsert (coalesce)::

    g.V(<from_id>)
     .outE('<edge_label>')
     .where(__.inV().hasId(<to_id>))
     .fold()
     .coalesce(
         __.unfold(),
         __.addE('<edge_label>')
           .from_(__.V(<from_id>))
           .to(__.V(<to_id>))
     )
     .id()
     .next()
"""

from __future__ import annotations

import logging
from typing import Any

from gremlin_python.driver import serializer
from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
from gremlin_python.process.anonymous_traversal import traversal
from gremlin_python.process.graph_traversal import GraphTraversalSource, __
from gremlin_python.process.traversal import T

logger = logging.getLogger(__name__)


class GremlinClientError(RuntimeError):
    """Raised when a Neptune / Gremlin operation fails."""


class NeptuneClient:
    """
    Manages a Gremlin connection to Amazon Neptune and exposes
    idempotent vertex and edge upsert operations.

    Parameters
    ----------
    endpoint : str
        Neptune cluster endpoint hostname (without scheme or port),
        e.g. ``"my-cluster.cluster-xxxxxx.us-east-1.neptune.amazonaws.com"``.
    port : int, optional
        Neptune Gremlin port.  Defaults to ``8182``.

    Examples
    --------
    >>> client = NeptuneClient(endpoint="cluster.us-east-1.neptune.amazonaws.com")
    >>> client.connect()
    >>> vid = client.upsert_vertex(
    ...     label="Model",
    ...     unique_key="name",
    ...     unique_value="GPT-4",
    ...     properties={"description": "Large language model by OpenAI"},
    ... )
    >>> eid = client.upsert_edge(
    ...     from_id=paper_vid,
    ...     edge_label="INTRODUCES",
    ...     to_id=vid,
    ... )
    >>> client.close()
    """

    def __init__(self, endpoint: str, port: int = 8182) -> None:
        self._endpoint = endpoint
        self._port     = port
        self._conn:  DriverRemoteConnection | None = None
        self._g:     GraphTraversalSource   | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open a WebSocket Gremlin connection to Neptune.

        The connection is stored at instance level for reuse across
        multiple traversal calls.  Call :meth:`close` to release it.

        Raises
        ------
        GremlinClientError
            If the WebSocket connection cannot be established.
        """
        ws_url = f"wss://{self._endpoint}:{self._port}/gremlin"
        logger.info("Connecting to Neptune at %s", ws_url)
        try:
            self._conn = DriverRemoteConnection(
                ws_url,
                "g",
                message_serializer=serializer.GraphSONSerializersV2d0(),
            )
            self._g = traversal().withRemote(self._conn)
            logger.info("Neptune connection established")
        except Exception as exc:
            raise GremlinClientError(
                f"Failed to connect to Neptune at {ws_url}: {exc}"
            ) from exc

    def close(self) -> None:
        """
        Close the Gremlin WebSocket connection gracefully.

        Safe to call even if :meth:`connect` was never called or if
        the connection is already closed.
        """
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("Neptune connection closed")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error closing Neptune connection: %s", exc)
            finally:
                self._conn = None
                self._g    = None

    # ------------------------------------------------------------------
    # Vertex operations
    # ------------------------------------------------------------------

    def upsert_vertex(
        self,
        label: str,
        unique_key: str,
        unique_value: str,
        properties: dict[str, Any],
    ) -> str:
        """
        Idempotently create or update a vertex.

        Uses the ``fold().coalesce(unfold(), addV())`` pattern to avoid
        duplicate vertices when the same paper is reprocessed.  After the
        vertex is located or created, all ``properties`` are applied via
        chained ``.property()`` steps.

        Gremlin traversal (conceptual)::

            g.V()
             .has('<label>', '<unique_key>', '<unique_value>')
             .fold()
             .coalesce(
                 __.unfold(),
                 __.addV('<label>').property('<unique_key>', '<unique_value>')
             )
             .property('k1', 'v1')
             .property('k2', 'v2')
             .id()
             .next()

        Parameters
        ----------
        label : str
            Vertex label, e.g. ``"Paper"``, ``"Author"``, ``"Model"``.
        unique_key : str
            Property name used as the uniqueness key, e.g. ``"paper_id"``.
        unique_value : str
            Value of the uniqueness key.
        properties : dict[str, Any]
            Additional properties to set on the vertex.  Existing
            properties with the same name are overwritten.

        Returns
        -------
        str
            The Neptune-assigned vertex ID.

        Raises
        ------
        GremlinClientError
            If the traversal fails or the client is not connected.
        """
        self._assert_connected()

        try:
            # Build the base coalesce traversal
            t = (
                self._g.V()
                .has(label, unique_key, unique_value)
                .fold()
                .coalesce(
                    __.unfold(),
                    __.addV(label).property(unique_key, unique_value),
                )
            )

            # Chain extra properties
            for prop_key, prop_val in properties.items():
                if prop_val is not None:
                    t = t.property(prop_key, str(prop_val))

            vertex_id = str(t.id_().next())
            logger.debug(
                "Upserted vertex label=%s key=%s val=%s id=%s",
                label, unique_key, unique_value, vertex_id,
            )
            return vertex_id

        except Exception as exc:
            raise GremlinClientError(
                f"upsert_vertex failed for {label}:{unique_key}={unique_value}: {exc}"
            ) from exc

    def get_vertex_id(
        self,
        label: str,
        key: str,
        value: str,
    ) -> str | None:
        """
        Return the vertex ID for an existing vertex, or ``None`` if not found.

        Parameters
        ----------
        label : str
            Vertex label to search under.
        key : str
            Property name to filter on.
        value : str
            Property value to match.

        Returns
        -------
        str or None
            Neptune vertex ID string, or ``None`` if no matching vertex exists.

        Raises
        ------
        GremlinClientError
            If the traversal fails.
        """
        self._assert_connected()

        try:
            results = (
                self._g.V()
                .has(label, key, value)
                .id_()
                .toList()
            )
            if results:
                return str(results[0])
            return None

        except Exception as exc:
            raise GremlinClientError(
                f"get_vertex_id failed for {label}:{key}={value}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        from_id: str,
        edge_label: str,
        to_id: str,
        properties: dict[str, Any] | None = None,
    ) -> str:
        """
        Idempotently create or retrieve a directed edge between two vertices.

        Uses the ``fold().coalesce(unfold(), addE())`` pattern.  If an
        edge with the same label already exists between ``from_id`` and
        ``to_id`` it is returned unchanged; otherwise it is created.

        Gremlin traversal (conceptual)::

            g.V(<from_id>)
             .outE('<edge_label>')
             .where(__.inV().hasId(<to_id>))
             .fold()
             .coalesce(
                 __.unfold(),
                 __.addE('<edge_label>')
                   .from_(__.V(<from_id>))
                   .to(__.V(<to_id>))
             )
             .property('k1', 'v1')
             .id()
             .next()

        Parameters
        ----------
        from_id : str
            Vertex ID of the source (out-vertex).
        edge_label : str
            Edge label, e.g. ``"INTRODUCES"``, ``"AUTHORED_BY"``.
        to_id : str
            Vertex ID of the target (in-vertex).
        properties : dict[str, Any] | None, optional
            Optional edge properties.

        Returns
        -------
        str
            The Neptune-assigned edge ID.

        Raises
        ------
        GremlinClientError
            If the traversal fails or either vertex ID does not exist.
        """
        self._assert_connected()
        properties = properties or {}

        try:
            t = (
                self._g.V(from_id)
                .outE(edge_label)
                .where(__.inV().hasId(to_id))
                .fold()
                .coalesce(
                    __.unfold(),
                    __.addE(edge_label)
                    .from_(__.V(from_id))
                    .to(__.V(to_id)),
                )
            )

            for prop_key, prop_val in properties.items():
                if prop_val is not None:
                    t = t.property(prop_key, str(prop_val))

            edge_id = str(t.id_().next())
            logger.debug(
                "Upserted edge %s -[%s]-> %s id=%s",
                from_id, edge_label, to_id, edge_id,
            )
            return edge_id

        except Exception as exc:
            raise GremlinClientError(
                f"upsert_edge failed for {from_id} -[{edge_label}]-> {to_id}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_connected(self) -> None:
        """Raise ``GremlinClientError`` if the connection is not open."""
        if self._g is None or self._conn is None:
            raise GremlinClientError(
                "NeptuneClient is not connected. Call connect() first."
            )
