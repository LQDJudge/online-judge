import os
import time

from django.core.management.base import BaseCommand, CommandError

from judge.ml.training import MODELS, get_trainer


class Command(BaseCommand):
    help = "Train ML embeddings and save to .npz"

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            choices=list(MODELS.keys()),
            help="Which model to train",
        )
        parser.add_argument(
            "--output",
            type=str,
            help="Path to save .npz file (for single model)",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Train all models",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            help="Directory to save .npz files (for --all, saves as <dir>/<model>/embeddings.npz)",
        )
        parser.add_argument(
            "--data-path",
            type=str,
            required=True,
            help="Directory containing problems.csv, profiles.csv, submissions.csv",
        )
        parser.add_argument("--iterations", type=int, default=2000)
        parser.add_argument("--embedding-dim", type=int, default=50)
        parser.add_argument("--lr", type=float, default=150.0)
        parser.add_argument(
            "--log-path", type=str, help="Save training loss plots to this path"
        )

    def handle(self, *args, **options):
        data_path = options["data_path"]
        if not os.path.isdir(data_path):
            raise CommandError(f"Data path does not exist: {data_path}")

        if options["all"]:
            if not options["output_dir"]:
                raise CommandError("--all requires --output-dir")
            for model_name in MODELS:
                output_npz = os.path.join(
                    options["output_dir"], model_name, "embeddings.npz"
                )
                self._train(model_name, data_path, output_npz, options)
            self.stdout.write(
                self.style.SUCCESS(
                    f"All models trained. Import with:\n"
                    f"  python manage.py import_embeddings --all --dir {options['output_dir']}"
                )
            )
        elif options["model"]:
            if not options["output"]:
                raise CommandError("--model requires --output")
            self._train(options["model"], data_path, options["output"], options)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Import with:\n"
                    f"  python manage.py import_embeddings --model {options['model']} --file {options['output']}"
                )
            )
        else:
            raise CommandError("Use --model/--output or --all/--output-dir")

    def _train(self, model_name, data_path, output_npz, options):
        self.stdout.write(f"Training {model_name}...")
        start = time.time()

        train_model = get_trainer(model_name)
        train_model(
            model_name,
            data_path=data_path,
            embedding_dim=options["embedding_dim"],
            iterations=options["iterations"],
            lr=options["lr"],
            log_path=options.get("log_path"),
            output_npz=output_npz,
        )

        self.stdout.write(
            f"  {model_name} completed in {time.time() - start:.1f}s → {output_npz}"
        )
