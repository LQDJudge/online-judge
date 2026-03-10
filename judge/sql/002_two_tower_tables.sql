-- Two Tower Model Vector Embedding Tables
-- Idempotent: safe to re-run (uses IF NOT EXISTS)
-- Requires MariaDB 11.7+

CREATE TABLE IF NOT EXISTS ml_problem_embedding_tt (
    problem_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ml_user_embedding_tt (
    user_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;
