import json
import os
import subprocess
import tempfile
import time

import numpy as np
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from judge.ml.vector_store import TABLE_MAP

EMBEDDING_DIM = 50


def upsert_embeddings(table, id_col, embeddings, batch_size=5000):
    """
    Replace all embeddings in a table using staging + atomic swap.

    1. Create staging table (no vector index — fast inserts)
    2. Bulk insert all embeddings
    3. Build HNSW vector index on staging table
    4. Atomic RENAME TABLE swap (live table serves until this instant)
    5. Drop old table

    ~3x faster than INSERT ON DUPLICATE KEY UPDATE with live index,
    and zero downtime (only 10-20ms during the rename).
    """
    t0 = time.time()
    items = [(int(k), json.dumps(v.tolist())) for k, v in embeddings.items()]
    total = len(items)
    staging = f"_staging_{table}"
    old = f"_old_{table}"

    print(f"  Importing {total} rows into {table}...")

    with connection.cursor() as c:
        # Disable statement timeout for long-running operations (e.g. index build)
        c.execute("SET SESSION max_statement_time = 0")

        # 1. Create staging table without vector index
        t1 = time.time()
        c.execute(f"DROP TABLE IF EXISTS {staging}")
        c.execute(f"DROP TABLE IF EXISTS {old}")
        c.execute(
            f"CREATE TABLE {staging} ("
            f"  {id_col} INT NOT NULL PRIMARY KEY,"
            f"  embedding VECTOR({EMBEDDING_DIM}) NOT NULL,"
            f"  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
            f") ENGINE=InnoDB"
        )
        print(f"    [1] Create staging table: {time.time() - t1:.2f}s")

        # 2. Bulk insert (fast — no vector index overhead)
        t2 = time.time()
        for i in range(0, total, batch_size):
            batch = items[i : i + batch_size]
            values_parts = []
            params = []
            for entity_id, vec_str in batch:
                values_parts.append("(%s, Vec_FromText(%s))")
                params.extend([entity_id, vec_str])
            c.execute(
                f"INSERT INTO {staging} ({id_col}, embedding) "
                f"VALUES {', '.join(values_parts)}",
                params,
            )
        print(f"    [2] Bulk insert {total} rows: {time.time() - t2:.2f}s")

        # 3. Build vector index
        t3 = time.time()
        c.execute(
            f"ALTER TABLE {staging} ADD VECTOR INDEX vec_idx (embedding) DISTANCE=cosine"
        )
        print(f"    [3] Build HNSW index: {time.time() - t3:.2f}s")

        # 4. Atomic swap
        t4 = time.time()
        c.execute(f"RENAME TABLE {table} TO {old}, {staging} TO {table}")
        print(f"    [4] Atomic swap: {time.time() - t4:.2f}s")

        # 5. Cleanup
        t5 = time.time()
        c.execute(f"DROP TABLE IF EXISTS {old}")
        print(f"    [5] Drop old table: {time.time() - t5:.2f}s")

    print(f"    Total: {time.time() - t0:.2f}s")


class Command(BaseCommand):
    help = "Import .npz embedding files into MariaDB vector tables"

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            choices=list(TABLE_MAP.keys()),
            help="Which model to import",
        )
        parser.add_argument(
            "--file",
            type=str,
            help="Path to .npz file",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Import all models from --dir or --from-modal",
        )
        parser.add_argument(
            "--dir",
            type=str,
            help="Directory containing model subdirs (for --all)",
        )
        parser.add_argument(
            "--from-modal",
            type=str,
            nargs="?",
            const="lqdoj-ml-volume",
            default=None,
            help="Download from Modal volume and import (default volume: lqdoj-ml-volume)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
        )

    def handle(self, *args, **options):
        if options["from_modal"] and options.get("all"):
            self._import_from_modal(options["from_modal"], options["batch_size"])
        elif options["all"]:
            if not options["dir"]:
                raise CommandError("--all requires --dir or --from-modal")
            self._import_all_from_dir(options["dir"], options["batch_size"])
        elif options["model"] and options["file"]:
            self._import_model(options["model"], options["file"], options["batch_size"])
        else:
            raise CommandError("Use --model/--file, --all/--dir, or --all --from-modal")

    def _import_from_modal(self, volume_name, batch_size):
        with tempfile.TemporaryDirectory() as tmpdir:
            for model_name in TABLE_MAP:
                remote_path = f"/{model_name}/embeddings.npz"
                local_path = os.path.join(tmpdir, model_name, "embeddings.npz")
                os.makedirs(os.path.dirname(local_path), exist_ok=True)

                self.stdout.write(f"Downloading {volume_name}:{remote_path}...")
                result = subprocess.run(
                    ["modal", "volume", "get", volume_name, remote_path, local_path],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping {model_name}: {result.stderr.strip()}"
                        )
                    )
                    continue
                self._import_model(model_name, local_path, batch_size)

    def _import_all_from_dir(self, dir_path, batch_size):
        for model_name in TABLE_MAP:
            npz_path = os.path.join(dir_path, model_name, "embeddings.npz")
            if os.path.exists(npz_path):
                self._import_model(model_name, npz_path, batch_size)
            else:
                self.stdout.write(
                    self.style.WARNING(f"Skipping {model_name}: {npz_path} not found")
                )

    def _import_model(self, model_name, npz_path, batch_size):
        self.stdout.write(f"Loading {npz_path}...")
        data = np.load(npz_path, allow_pickle=True)
        user_key, problem_key = data.files

        uid_embeddings = data[user_key].item()
        pid_embeddings = data[problem_key].item()

        tables = TABLE_MAP[model_name]
        upsert_embeddings(tables["user"], "user_id", uid_embeddings, batch_size)
        upsert_embeddings(tables["problem"], "problem_id", pid_embeddings, batch_size)

        self.stdout.write(self.style.SUCCESS(f"Imported {model_name}"))
