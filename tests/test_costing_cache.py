"""Pricing caches must follow inputs, not just a model name and test label."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import pickle
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sdbench.config import CostingConfig, get_sdbench_config
from sdbench.costing import cache


def config(**changes):
    original = CostingConfig.from_sdbench_config(get_sdbench_config("synthetic"))
    return replace(original, costing_method="llm_only", **changes)


class NotFound(Exception):
    pass


class MemoryContainer:
    def __init__(self):
        self.items = {}

    def read_item(self, item, partition_key):
        if item not in self.items:
            raise NotFound()
        return self.items[item]

    def upsert_item(self, body):
        self.items[body["id"]] = body


class CostingCacheTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = config()
        self.store = cache.CosmosDBCache(self.config)
        self.store._container = MemoryContainer()
        self.price = {"test_name": "CBC", "cost": 35, "method": "llm_only",
                      "cpt_estimate": None, "llm": None, "explanation": "fixture"}

    def test_same_inputs_hit_and_names_normalize(self):
        with patch("builtins.print"):
            self.store.set(self.config, " CBC ", self.price)
        self.assertEqual(self.store.get(self.config, "cbc"), self.price)

    def test_effective_model_arguments_change_key_but_order_does_not(self):
        first = replace(self.config, costing_model_args_tuple=(("extra_body", {"a": 1, "b": 2}),))
        reordered = replace(first, costing_model_args_tuple=(("extra_body", {"b": 2, "a": 1}),))
        changed = replace(first, costing_model_args_tuple=(("extra_body", {"a": 3, "b": 2}),))
        self.assertEqual(cache.pricing_identity(first), cache.pricing_identity(reordered))
        self.assertNotEqual(cache.pricing_identity(first), cache.pricing_identity(changed))
        self.assertNotEqual(cache.pricing_identity(first), cache.pricing_identity(replace(first, costing_model="other-model")))

    def test_nonpricing_run_options_do_not_invalidate_a_test_price(self):
        changed = replace(self.config, clinician_cost=900, debug=True, costing_parallelism=99)
        self.assertEqual(cache.pricing_identity(self.config), cache.pricing_identity(changed))

    def test_provider_routes_and_endpoints_invalidate(self):
        initial = cache.pricing_identity(self.config)
        with patch.dict(os.environ, {"SDBENCH_FORCE_AZURE": "1", "AZURE_OPENAI_ENDPOINT": "https://first.test"}):
            azure = cache.pricing_identity(self.config)
            self.assertNotEqual(initial, azure)
            with patch.dict(os.environ, {"AZURE_OPENAI_ENDPOINT": "https://second.test"}):
                self.assertNotEqual(azure, cache.pricing_identity(self.config))

    def test_charge_master_changed_at_same_path_invalidates_even_same_file_size(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "prices.xlsx"
            source.write_bytes(b"price 100")
            grounded = replace(self.config, costing_method="unified_cpt_llm", costing_source_file_path=str(source))
            initial = cache.pricing_identity(grounded)
            source.write_bytes(b"price 200")
            self.assertNotEqual(initial, cache.pricing_identity(grounded))

    def test_source_resolves_again_when_data_root_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            grounded = replace(self.config, costing_method="unified_cpt_llm", costing_source_file_path="prices.xlsx")
            identities = []
            for suffix in ("one", "two"):
                root = Path(directory) / suffix
                (root / "costing_data").mkdir(parents=True)
                (root / "costing_data/prices.xlsx").write_text(suffix)
                with patch.dict(os.environ, {"SDBENCH_DATA_PATH": str(root)}):
                    identities.append(cache.pricing_identity(grounded))
            self.assertNotEqual(*identities)

    def test_unchanged_large_source_is_hashed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "prices.xlsx"
            source.write_bytes(b"public pricing fixture")
            with patch.object(cache.hashlib, "file_digest", wraps=cache.hashlib.file_digest) as hashing:
                first = cache.file_fingerprint(source)
                self.assertEqual(first, cache.file_fingerprint(source))
                self.assertEqual(hashing.call_count, 1)
                source.write_bytes(b"new public pricing fixture")
                self.assertNotEqual(first, cache.file_fingerprint(source))
                self.assertEqual(hashing.call_count, 2)

    def test_implementation_or_prompt_change_invalidates(self):
        before = cache.pricing_identity(self.config)
        with patch.dict(cache._IMPLEMENTATION_FINGERPRINTS, {"llm_estimater.py": "new-process-prompt"}):
            self.assertNotEqual(before, cache.pricing_identity(self.config))

    def test_implementation_identity_stays_with_loaded_code_until_process_restart(self):
        before = cache.pricing_identity(self.config)
        with patch.object(cache, "file_fingerprint", side_effect=AssertionError("No source reread")):
            self.assertEqual(before, cache.pricing_identity(self.config))

    def test_llm_only_does_not_require_charge_master(self):
        self.assertTrue(cache.pricing_identity(replace(self.config, costing_source_file_path="/absent/prices.xlsx")))

    def test_disabled_cache_does_not_hash_files_or_create_a_client(self):
        with patch.object(cache, "pricing_identity", side_effect=AssertionError("must not hash")), patch.dict(cache._clients, {}, clear=True):
            self.assertIsNone(cache.get_cached_cost(self.config, "CBC"))
            cache.set_cached_cost(self.config, "CBC", self.price)
            self.assertEqual(cache._clients, {})

    def test_connection_reuse_does_not_hash_nested_model_args(self):
        enabled = replace(self.config, costing_cache_cosmosdb_enabled=True,
                          costing_model_args_tuple=(("extra_body", {"reasoning": {"effort": "high"}}),))
        with patch.dict(cache._clients, {}, clear=True), patch.object(cache, "CosmosDBCache") as constructor:
            cache.get_cache(enabled)
            cache.get_cache(enabled)
            constructor.assert_called_once_with(enabled)

    def test_old_cache_entries_are_missed_and_never_trusted_on_id_collision(self):
        legacy = f"{self.config.costing_method}:{self.config.costing_model}:cbc"
        self.store._container.items[legacy] = {**self.price, "cache_key": legacy}
        self.assertIsNone(self.store.get(self.config, "CBC"))
        self.store._container.read_item = Mock(return_value={**self.price, "cache_key": legacy})
        self.assertIsNone(self.store.get(self.config, "CBC"))

    def test_ids_cannot_collide_by_truncation_or_slash_replacement(self):
        keys = [cache.CosmosDBCache.normalize_key(self.config, name) for name in ("a/b", "a_b", "🧬" * 1000 + "one", "🧬" * 1000 + "two")]
        ids = [cache.CosmosDBCache.make_cosmosdb_safe_id(key) for key in keys]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(len(value.encode()) == 64 for value in ids))

    def test_configuration_and_credentials_are_not_persisted(self):
        local = replace(self.config, costing_model="local-test", costing_model_args_tuple=(
            ("local_model_base_url", "https://local.test/v1"),
            ("local_model_api_key", "PRIVATE_API_KEY"),
            ("local_model_headers", {"Authorization": "PRIVATE_BEARER", "X-Route": "staging"}),
        ))
        with patch("builtins.print"):
            self.store.set(local, "CBC", self.price)
        stored = json.dumps(self.store._container.items)
        self.assertNotIn("PRIVATE", stored)
        self.assertNotIn("config", stored)
        changed_secret = replace(local, costing_model_args_tuple=tuple((key, "ROTATED" if key == "local_model_api_key" else value) for key, value in local.costing_model_args_tuple))
        self.assertEqual(cache.pricing_identity(local), cache.pricing_identity(changed_secret))

    def test_invalid_prices_and_failures_are_not_cached(self):
        for value in (None, -1, float("nan"), float("inf"), True):
            self.store.set(self.config, "CBC", {**self.price, "cost": value})
        self.store.set(self.config, "CBC", {**self.price, "method": "failure"})
        self.assertEqual(self.store._container.items, {})
        with patch("builtins.print"):
            self.store.set(self.config, "CBC", self.price)
        next(iter(self.store._container.items.values()))["cost"] = float("nan")
        self.assertIsNone(self.store.get(self.config, "CBC"))


try:
    from sdbench.costing import cpt_matcher
except ImportError:
    cpt_matcher = None


@unittest.skipIf(cpt_matcher is None, "requires the cpt_costing extra")
class CPTDiskCacheTests(unittest.TestCase):
    def matcher(self):
        matcher = object.__new__(cpt_matcher.CPTFuzzyMatcher)
        matcher._df = cpt_matcher.pd.DataFrame({"Procedure Name": ["test A", "test B"]})
        matcher._get_embeddings = Mock(return_value=[[1., 0.], [0., 1.]])
        return matcher

    def test_changed_same_row_count_source_and_embedding_route_rebuild_indexes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "fixture"}, clear=True):
            source = Path(directory) / "prices.xlsx"
            source.write_bytes(b"first source")
            Path(str(source) + ".faiss.index").write_bytes(b"unversioned legacy index")
            cfg = config(costing_source_file_path=str(source))
            frame = cpt_matcher.pd.DataFrame({"Procedure Name": ["test A", "test B"]})
            with patch.object(cpt_matcher, "OpenAI"), patch.object(cpt_matcher.CPTFuzzyMatcher, "_load_and_process_charge_data", return_value=frame), patch.object(cpt_matcher.CPTFuzzyMatcher, "_get_embeddings", return_value=[[1., 0.], [0., 1.]]) as embeddings, patch("builtins.print"):
                cpt_matcher.CPTFuzzyMatcher(cfg)
                self.assertEqual(embeddings.call_count, 1)
                cpt_matcher.CPTFuzzyMatcher(cfg)
                self.assertEqual(embeddings.call_count, 1)
                source.write_bytes(b"other source")
                cpt_matcher.CPTFuzzyMatcher(cfg)
                self.assertEqual(embeddings.call_count, 2)
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "fixture-router"}):
                    cpt_matcher.CPTFuzzyMatcher(cfg)
                self.assertEqual(embeddings.call_count, 3)
            self.assertEqual(len(list(Path(directory).glob("*.sdbench-*.faiss.index"))), 3)

    def test_corrupt_or_wrong_shape_disk_cache_rebuilds(self):
        for failure in ("corrupt-index", "wrong-row-index", "corrupt-pickle", "wrong-row-pickle", "invalid-vector"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
                embeddings = Path(directory) / "embeddings.pkl"
                index = Path(directory) / "faiss.index"
                if failure == "corrupt-index":
                    index.write_bytes(b"interrupted index")
                elif failure == "wrong-row-index":
                    wrong = cpt_matcher.faiss.IndexFlatIP(2)
                    wrong.add(cpt_matcher.np.asarray([[1., 0.]], dtype="float32"))
                    cpt_matcher.faiss.write_index(wrong, str(index))
                elif failure == "corrupt-pickle":
                    embeddings.write_bytes(b"interrupted pickle")
                else:
                    vectors = [[1., 0.]] if failure == "wrong-row-pickle" else [[float("nan"), 0.], [0., 1.]]
                    embeddings.write_bytes(pickle.dumps({"embeddings": vectors}))
                matcher = self.matcher()
                result = matcher._load_or_create_embeddings_and_index(str(embeddings), str(index))
                self.assertEqual(result.ntotal, 2)
                matcher._get_embeddings.assert_called_once()
                self.assertEqual(cpt_matcher.faiss.read_index(str(index)).ntotal, 2)
                self.assertEqual(len(pickle.loads(embeddings.read_bytes())["embeddings"]), 2)
                self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_interrupted_writes_leave_previous_cache_untouched(self):
        for failure in ("pickle", "index"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
                embeddings = Path(directory) / "embeddings.pkl"
                index = Path(directory) / "faiss.index"
                embeddings.write_bytes(b"old invalid pickle")
                index.write_bytes(b"old invalid index")
                if failure == "pickle":
                    def interrupted(data, output):
                        output.write(b"partial new pickle")
                        raise OSError("interrupted write")
                    target = patch.object(cpt_matcher.pickle, "dump", side_effect=interrupted)
                else:
                    def interrupted(data, path):
                        Path(path).write_bytes(b"partial new index")
                        raise OSError("interrupted write")
                    target = patch.object(cpt_matcher.faiss, "write_index", side_effect=interrupted)
                with target, self.assertRaisesRegex(OSError, "interrupted write"):
                    self.matcher()._load_or_create_embeddings_and_index(str(embeddings), str(index))
                self.assertEqual(index.read_bytes(), b"old invalid index")
                if failure == "pickle":
                    self.assertEqual(embeddings.read_bytes(), b"old invalid pickle")
                self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_concurrent_writers_publish_complete_files(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            embeddings = str(Path(directory) / "embeddings.pkl")
            index = str(Path(directory) / "faiss.index")
            def build(_):
                return self.matcher()._load_or_create_embeddings_and_index(embeddings, index)
            with ThreadPoolExecutor(max_workers=2) as executor:
                indexes = list(executor.map(build, range(2)))
            self.assertEqual([value.ntotal for value in indexes], [2, 2])
            self.assertEqual(cpt_matcher.faiss.read_index(index).ntotal, 2)
            self.assertEqual(len(pickle.loads(Path(embeddings).read_bytes())["embeddings"]), 2)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


@unittest.skipIf(cpt_matcher is None, "requires the cpt_costing extra")
class CPTFailureTests(unittest.TestCase):
    def matcher(self):
        matcher = object.__new__(cpt_matcher.CPTFuzzyMatcher)
        matcher._config = config()
        matcher._openai_client = Mock()
        matcher._embedding_model = "fixture"
        return matcher

    def test_embedding_provider_failure_never_becomes_cached_keyword_only_price(self):
        from sdbench.costing import costing
        matcher = self.matcher()
        matcher._openai_client.embeddings.create.side_effect = RuntimeError("provider offline")
        matcher.keyword_search = Mock(return_value=[{"cpt_code": "fixture"}])
        matcher.select_best_cpt = Mock(return_value={"selected_cpts": ["fixture"]})
        with patch.object(costing, "get_cpt_fuzzy_matcher", return_value=matcher), \
             patch.object(cache, "get_cached_cost", return_value=None), \
             patch.object(cache, "set_cached_cost") as saved, patch("builtins.print"):
            result = costing.estimate_unified_test_costs("CBC", matcher._config)
        self.assertEqual(result["method"], "failure")
        self.assertIsNone(result["cost"])
        saved.assert_not_called()
        matcher.keyword_search.assert_not_called()
        matcher.select_best_cpt.assert_not_called()

    def test_keyword_expansion_and_selection_provider_failures_are_explicit(self):
        matcher = self.matcher()
        with patch.object(cpt_matcher, "create_chat_completion", side_effect=RuntimeError("provider offline")), patch("builtins.print"):
            with self.assertRaises(cpt_matcher.CPTSearchError):
                matcher.generate_expanded_keywords("CBC")
            with self.assertRaises(cpt_matcher.CPTSearchError):
                matcher.select_best_cpt("CBC", [{"cpt_code": "1", "procedure_name": "CBC", "gross_charge": 25, "setting": "Both"}])

    def test_expanded_embeddings_cannot_silently_skip_a_failed_keyword(self):
        matcher = self.matcher()
        matcher.generate_expanded_keywords = Mock(return_value=["one", "two"])
        matcher._get_embeddings = Mock(side_effect=[cpt_matcher.CPTSearchError("batch failed"), [[1., 0.]], cpt_matcher.CPTSearchError("second failed")])
        matcher.keyword_search = Mock()
        with patch("builtins.print"), self.assertRaises(cpt_matcher.CPTSearchError):
            matcher.llm_expanded_search("CBC")
        matcher.keyword_search.assert_not_called()

    def test_genuine_no_match_retains_llm_fallback(self):
        from sdbench.costing import costing
        matcher = self.matcher()
        matcher.combined_search = Mock(return_value=[])
        with patch.object(costing, "get_cpt_fuzzy_matcher", return_value=matcher), \
             patch.object(cache, "get_cached_cost", return_value=None), \
             patch.object(cache, "set_cached_cost") as saved, \
             patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 25}), patch("builtins.print"):
            result = costing.estimate_unified_test_costs("CBC", matcher._config)
        self.assertEqual(result["method"], "llm_fallback")
        self.assertEqual(result["cost"], 25)
        saved.assert_called_once()

    def selection_result(self, text, finish_reason="stop"):
        from sdbench.costing import costing
        matcher = self.matcher()
        candidate = {"cpt_code": "71046", "procedure_name": "Chest X-ray", "gross_charge": 25, "setting": "Both"}
        matcher._df = cpt_matcher.pd.DataFrame({"Procedure Code (CPT / HCPCS)": ["71046"]})
        matcher.combined_search = Mock(return_value=[candidate])
        matcher.median_price = Mock(return_value=25)
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason, message=SimpleNamespace(content=text),
        )])
        with patch.object(costing, "get_cpt_fuzzy_matcher", return_value=matcher), \
             patch.object(cache, "get_cached_cost", return_value=None), \
             patch.object(cache, "set_cached_cost") as saved, \
             patch.object(costing.llm_estimater, "cost_test", return_value={"cost": 40}) as fallback, \
             patch.object(cpt_matcher, "create_chat_completion", return_value=response) as provider, \
             patch("builtins.print"):
            result = costing.estimate_unified_test_costs("Chest X-ray", matcher._config)
        return result, saved, fallback, provider

    def test_malformed_or_truncated_selection_never_caches_a_fallback_price(self):
        replies = (
            None, "", "REASONING: This might fit.",
            "REASONING: This might fit.\nSELECTED_CPT:",
            "REASONING: This might fit.\nSELECTED_CPT: 7104",
            "REASONING: This might fit.\nSELECTED_CPT 71046",
            "REASONING: Contradiction.\nSELECTED_CPT: NONE\nSELECTED_CPT: 71046",
        )
        for text in replies:
            with self.subTest(text=text):
                result, saved, fallback, _ = self.selection_result(text)
                self.assertEqual(result["method"], "failure")
                self.assertIsNone(result["cost"])
                saved.assert_not_called()
                fallback.assert_not_called()
        result, saved, fallback, _ = self.selection_result(
            "REASONING: This fits.\nSELECTED_CPT: 71046", finish_reason="length",
        )
        self.assertEqual(result["method"], "failure")
        saved.assert_not_called()
        fallback.assert_not_called()

    def test_documented_no_match_and_unavailable_valid_code_preserve_fallback(self):
        for selected in ("NONE", "99999"):
            with self.subTest(selected=selected):
                result, saved, fallback, provider = self.selection_result(
                    f"REASONING: No suitable grounded match.\nSELECTED_CPT: {selected}",
                )
                self.assertEqual(result["method"], "llm_fallback")
                self.assertEqual(result["cost"], 40)
                saved.assert_called_once()
                fallback.assert_called_once()
                self.assertIn("SELECTED_CPT: NONE", provider.call_args.kwargs["messages"][1]["content"])

    def test_complete_selection_still_uses_grounded_price(self):
        result, saved, fallback, _ = self.selection_result(
            "REASONING: Standard two-view chest X-ray.\nSELECTED_CPT: 71046",
        )
        self.assertEqual(result["method"], "cpt_lookup")
        self.assertEqual(result["cost"], 25)
        saved.assert_called_once()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
