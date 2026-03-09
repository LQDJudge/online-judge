-- ML Vector Embedding Tables
-- Idempotent: safe to re-run (uses IF NOT EXISTS)
-- Requires MariaDB 11.7+

CREATE TABLE IF NOT EXISTS ml_problem_embedding_cf (
    problem_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ml_problem_embedding_cf_time (
    problem_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ml_user_embedding_cf (
    user_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ml_user_embedding_cf_time (
    user_id INT NOT NULL PRIMARY KEY,
    embedding VECTOR(50) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    VECTOR INDEX vec_idx (embedding) DISTANCE=cosine
) ENGINE=InnoDB;
