"""Cost estimates keyed by pricing inputs, with an optional CosmosDB store."""

import functools
import hashlib
import json
import os
from pathlib import Path
import threading
import traceback
from typing import TYPE_CHECKING, Any

from sdbench.config import CostingConfig
from sdbench.costing.cost_types import valid_cost
from sdbench.models import _configure_completion, _parse_headers

if TYPE_CHECKING:
    from sdbench.costing.cost_types import CostEstimate


CACHE_VERSION = 2
_clients: dict[tuple[Any, ...], "CosmosDBCache"] = {}
_clients_lock = threading.Lock()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _file_state(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@functools.lru_cache(maxsize=64)
def _file_digest(path: Path, state: tuple[int, ...]) -> str:
    # Stat on every lookup, but reread large charge masters only when changed.
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if _file_state(path) != state:
        raise ValueError(f"Pricing source changed while being read: {path}")
    return digest


def file_fingerprint(path: str | Path) -> str:
    resolved = Path(path).resolve()
    return _file_digest(resolved, _file_state(resolved))


# A running interpreter keeps its loaded code. Re-reading changed source files on
# every lookup would incorrectly label old behavior with the new implementation.
_IMPLEMENTATION_FINGERPRINTS = {
    name: file_fingerprint(Path(__file__).parent / name)
    for name in ("costing.py", "cpt_matcher.py", "llm_estimater.py", "llm_batch_estimator.py", "../models.py")
}


def embedding_identity(config: CostingConfig) -> str:
    """Identity for the charge-master rows, embedding route and preprocessing."""
    if os.getenv("OPENROUTER_API_KEY"):
        provider = {
            "url": "https://openrouter.ai/api/v1",
            "model": "openai/text-embedding-3-large",
        }
    else:
        provider = {
            "url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "model": "text-embedding-3-large",
        }
    return _digest({
        "version": CACHE_VERSION,
        "source": file_fingerprint(config.resolved_costing_source_file_path),
        "provider": provider,
        "implementation": _IMPLEMENTATION_FINGERPRINTS["cpt_matcher.py"],
    })


def pricing_identity(config: CostingConfig) -> str:
    """Fingerprint effective pricing inputs without persisting credentials or paths.

    Provider model/deployment names must still be pinned by the caller: their remote
    weights cannot be fingerprinted offline. Pricing files and provider settings
    must remain fixed while a price request is in flight. Restart the interpreter
    after changing implementation files; hot reloads are not supported.
    """
    args = config.costing_model_args
    credential_variable = args.pop("openrouter_api_key_var", "OPENROUTER_API_KEY")
    provider, model, args = _configure_completion(config.costing_model, **args)
    route: dict[str, Any] = {"provider": provider, "model": model}
    if provider == "azure":
        route.update(
            endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
        )
    elif provider == "local":
        route["endpoint"] = args.pop("local_model_base_url", None) or os.getenv(
            "SDBENCH_LOCAL_BASE_URL"
        )
        args.pop("local_model_api_key", None)
        headers = _parse_headers(
            args.pop("local_model_headers", None) or os.getenv("SDBENCH_LOCAL_HEADERS")
        ) or {}
        route["headers"] = {
            key: value for key, value in headers.items()
            if key.lower() not in {"authorization", "api-key", "x-api-key"}
        }
    else:
        route["credential_variable"] = credential_variable

    implementation_files = ["costing.py", "../models.py"]
    if config.costing_method == "batch_llm_only":
        implementation_files.append("llm_batch_estimator.py")
    else:
        implementation_files.append("llm_estimater.py")
    inputs: dict[str, Any] = {
        "version": CACHE_VERSION,
        "method": config.costing_method,
        "route": route,
        "args": args,
        "implementation": [_IMPLEMENTATION_FINGERPRINTS[name] for name in implementation_files],
    }
    if config.costing_method == "unified_cpt_llm":
        inputs["grounding"] = embedding_identity(config)
    return _digest(inputs)


def get_cache(config: CostingConfig) -> "CosmosDBCache":
    """Reuse connections independently of potentially nested model arguments."""
    if not config.costing_cache_cosmosdb_enabled:
        return CosmosDBCache(config)
    key = (
        config.costing_cache_cosmosdb_endpoint,
        config.costing_cache_cosmosdb_database,
        config.costing_cache_cosmosdb_container,
        config.costing_cache_use_managed_identity,
    )
    with _clients_lock:
        if key not in _clients:
            _clients[key] = CosmosDBCache(config)
        return _clients[key]


def get_cached_cost(config: CostingConfig, test_name: str) -> "CostEstimate | None":
    """Look up a cached cost estimate for the given test name, returning None on error."""
    try:
        return get_cache(config).get(config, test_name)
    except Exception as e:
        print(f"Error accessing cache: {e}")
        traceback.print_exc()
        return None


def set_cached_cost(config: CostingConfig, test_name: str, cost_estimate: "CostEstimate") -> None:
    """Persist a cost estimate to the cache, silently ignoring errors."""
    try:
        get_cache(config).set(config, test_name, cost_estimate)
    except Exception as e:
        print(f"Error setting cache: {e}")
        traceback.print_exc()


class CosmosDBCache:
    """CosmosDB-backed cache for test cost estimates."""

    def __init__(self, config: CostingConfig):
        """Initialize the cache, connecting to CosmosDB if enabled in config."""
        self._debug = config.debug
        self._container: Any | None = None

        if not config.costing_cache_cosmosdb_enabled:
            return

        if not (
            config.costing_cache_cosmosdb_endpoint
            and config.costing_cache_cosmosdb_database
            and config.costing_cache_cosmosdb_container
        ):
            raise ValueError(
                "CosmosDB cache enabled but configuration incomplete. Required: endpoint, database, container"
            )

        # Imported lazily so the Azure Cosmos SDK is only required when the cache is enabled.
        try:
            from azure.cosmos import CosmosClient
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
        except ImportError as e:
            raise ImportError(
                "CosmosDB costing cache is enabled (costing_cache_cosmosdb_enabled=True) but the "
                "Azure Cosmos SDK is not installed. Install the optional dependency with "
                "`uv sync --extra azure` (or `pip install 'sdbench[azure]'`), or disable the cache."
            ) from e

        try:
            print(
                f"Initializing CosmosDB cache: {config.costing_cache_cosmosdb_endpoint}/{config.costing_cache_cosmosdb_database}/{config.costing_cache_cosmosdb_container}"
            )
            credential = (
                ManagedIdentityCredential()
                if config.costing_cache_use_managed_identity
                else DefaultAzureCredential()
            )
            client = CosmosClient(config.costing_cache_cosmosdb_endpoint, credential)
            database = client.get_database_client(config.costing_cache_cosmosdb_database)
            self._container = database.get_container_client(config.costing_cache_cosmosdb_container)
            print("CosmosDB cache initialized successfully")
        except Exception as e:
            raise ValueError(f"Failed to initialize CosmosDB costing cache: {e}") from e

    @property
    def enabled(self) -> bool:
        """Check if the cache is enabled and ready to use."""
        return self._container is not None

    @staticmethod
    def normalize_key(config: CostingConfig, key: str) -> str:
        """A versioned identity; old method/model/name-only entries cannot match."""
        return f"v{CACHE_VERSION}:{pricing_identity(config)}:{_digest(key.lower().strip())}"

    @staticmethod
    def make_cosmosdb_safe_id(normalized_key: str) -> str:
        """Fixed ASCII IDs avoid forbidden characters and truncation collisions."""
        return hashlib.sha256(normalized_key.encode()).hexdigest()

    def get(self, config: CostingConfig, key: str) -> "CostEstimate | None":
        """Look up a cached value using a point read.

        Args:
            config: The costing configuration used
            key: The cache key (will be normalized automatically)

        Returns:
            Cached data if found, None otherwise
        """
        if not self.enabled:
            if self._debug:
                print(f"Cache skipped for test: {key}")
            return None

        normalized_key = self.normalize_key(config, key)
        safe_id = self.make_cosmosdb_safe_id(normalized_key)

        try:
            # Use point read for efficient lookup (requires id and partition key)
            # Misses are indicated by exceptions
            cached_data = self._container.read_item(item=safe_id, partition_key=safe_id)  # pyright: ignore[reportOptionalMemberAccess]

            # Validate the stored cache_key matches our normalized key
            stored_cache_key = cached_data.get("cache_key")
            if stored_cache_key != normalized_key:
                if self._debug:
                    print(
                        f"⚠️ Cache key mismatch: expected '{normalized_key}', got '{stored_cache_key}'"
                    )
                return None

            # Return the cached result, removing CosmosDB-specific fields
            cached_data = dict(cached_data)
            # Remove config field that was added during caching
            cached_data.pop("config", None)
            # Remove CosmosDB metadata fields
            cached_data.pop("id", None)
            cached_data.pop("_rid", None)
            cached_data.pop("_self", None)
            cached_data.pop("_etag", None)
            cached_data.pop("_attachments", None)
            cached_data.pop("_ts", None)
            cached_data.pop("cache_key", None)

            if not valid_cost(cached_data.get("cost")) or cached_data.get("method") == "failure":
                return None
            if self._debug:
                print(f"✓ Cache hit for: '{normalized_key}'")
            return cached_data  # type: ignore[return-value]

        except Exception as e:
            # Point read throws exception if item not found, which is normal
            if "NotFound" in str(type(e).__name__) or "ResourceNotFound" in str(type(e).__name__):
                if self._debug:
                    print(f"Cache miss for test: {normalized_key}")
                return None
            print(f"Warning: Failed to lookup cache for '{normalized_key}': {e}")
            return None

    def set(self, config: CostingConfig, key: str, value: "CostEstimate") -> None:
        """Save a value to the cache.

        Args:
            config: The costing configuration used
            key: The cache key (will be normalized automatically)
            value: The data to cache
        """
        if (
            not self.enabled
            or not valid_cost(value.get("cost"))
            or value.get("method") == "failure"
        ):
            return

        normalized_key = self.normalize_key(config, key)
        safe_id = self.make_cosmosdb_safe_id(normalized_key)

        try:
            # Prepare data for CosmosDB
            cache_data = dict(value)
            cache_data["id"] = safe_id  # CosmosDB-safe ID for storage
            cache_data["cache_key"] = normalized_key  # Original normalized key for matching

            # Upsert the item (insert or update)
            self._container.upsert_item(body=cache_data)  # pyright: ignore[reportOptionalMemberAccess]
            print(f"✓ Cached result for: '{normalized_key}' (stored as: '{safe_id}')")

        except Exception as e:
            print(f"Warning: Failed to save to cache for '{normalized_key}': {e}")
