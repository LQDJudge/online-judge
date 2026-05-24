-- Semantic problem search embedding tables
-- Idempotent: safe to re-run (uses IF NOT EXISTS)
-- Requires MariaDB 11.7+

CREATE TABLE IF NOT EXISTS ml_problem_semantic_embedding (
    problem_id INT NOT NULL PRIMARY KEY,
    model VARCHAR(100) NOT NULL,
    dims SMALLINT UNSIGNED NOT NULL,
    content_hash CHAR(64) NOT NULL,
    embedding VECTOR(768) NOT NULL,
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY model_dims_idx (model, dims),
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ml_problem_semantic_index_error (
    problem_id INT NOT NULL PRIMARY KEY,
    model VARCHAR(100) NOT NULL,
    dims SMALLINT UNSIGNED NOT NULL,
    content_hash CHAR(64) NOT NULL,
    error_text TEXT NOT NULL,
    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY model_dims_idx (model, dims)
) ENGINE=InnoDB;
