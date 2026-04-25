import numpy as np
import pytest


class TestComputeEmbedding:
    def test_returns_576_dim_vector(self):
        from edge.edge_cv_embed import compute_embedding
        fake_crop = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        emb = compute_embedding(fake_crop)
        assert emb.shape == (576,)
        assert emb.dtype == np.float32

    def test_same_image_same_embedding(self):
        from edge.edge_cv_embed import compute_embedding
        crop = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        e1 = compute_embedding(crop)
        e2 = compute_embedding(crop)
        np.testing.assert_array_equal(e1, e2)

    def test_different_images_different_embeddings(self):
        from edge.edge_cv_embed import compute_embedding
        c1 = np.zeros((224, 224, 3), dtype=np.uint8)
        c2 = np.full((224, 224, 3), 255, dtype=np.uint8)
        e1 = compute_embedding(c1)
        e2 = compute_embedding(c2)
        assert not np.allclose(e1, e2)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from edge.edge_cv_embed import cosine_similarity
        v = np.random.randn(576).astype(np.float32)
        assert cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-5)

    def test_orthogonal_vectors(self):
        from edge.edge_cv_embed import cosine_similarity
        a = np.zeros(576, dtype=np.float32)
        b = np.zeros(576, dtype=np.float32)
        a[0] = 1.0
        b[1] = 1.0
        assert cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-5)


class TestEmbeddingCache:
    def test_duplicate_detected(self):
        from edge.edge_cv_embed import EmbeddingCache
        cache = EmbeddingCache(max_size=100, threshold=0.95)
        v = np.random.randn(576).astype(np.float32)
        assert not cache.is_duplicate(v)
        cache.register(v)
        assert cache.is_duplicate(v)

    def test_different_not_duplicate(self):
        from edge.edge_cv_embed import EmbeddingCache
        cache = EmbeddingCache(max_size=100, threshold=0.95)
        v1 = np.random.randn(576).astype(np.float32)
        v2 = np.random.randn(576).astype(np.float32)
        cache.register(v1)
        assert not cache.is_duplicate(v2)

    def test_evicts_oldest(self):
        from edge.edge_cv_embed import EmbeddingCache
        cache = EmbeddingCache(max_size=3, threshold=0.95)
        vecs = [np.random.randn(576).astype(np.float32) for _ in range(4)]
        for v in vecs[:3]:
            cache.register(v)
        assert cache.is_duplicate(vecs[0])
        cache.register(vecs[3])
        assert not cache.is_duplicate(vecs[0])


class TestSerialize:
    def test_roundtrip(self):
        from edge.edge_cv_embed import embedding_to_b64, b64_to_embedding
        v = np.random.randn(576).astype(np.float32)
        encoded = embedding_to_b64(v)
        assert isinstance(encoded, str)
        decoded = b64_to_embedding(encoded)
        np.testing.assert_array_equal(v, decoded)
