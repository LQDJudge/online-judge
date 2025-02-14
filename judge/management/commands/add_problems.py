"""
Run python3 manage.py add_problems /path/to/root
Inside root folder, there's a metadata.json:
[
    {
        "problem_name": "Bé tập vẽ",
        "input_file": "DRAW.INP",
        "output_file": "DRAW.OUT",
        "time_limit": "1",
        "memory_limit": "512",
        "problem_code": "vttthithuthcsc01p1",
        "statement_file": "contest01/vttthithuthc01p1.md",
        "test_file": "contest01/DRAW.zip",
        "problem_author": "Flower_On_Stone",
        "points": 100,
        "group_id": 1
    },
    {
        "problem_name": "Another Problem",
        "input_file": "ANOTHER.INP",
        "output_file": "ANOTHER.OUT",
        "time_limit": "2",
        "memory_limit": "256",
        "problem_code": "anotherproblem01",
        "statement_file": "contest01/anotherproblem.md",
        "test_file": "contest01/ANOTHER.zip",
        "problem_author": "Another_Author",
        "points": 100,
        "group_id": 1
    }
]
"""

import json
import os
from zipfile import ZipFile
from django.core.management.base import BaseCommand
from django.core.files import File
from django.utils.timezone import now

from judge.models import Problem, ProblemData, Profile, ProblemTestCase, ProblemGroup
from judge.utils.problem_data import ProblemDataCompiler


class Command(BaseCommand):
    help = "Import problems from a JSON file in the specified root folder"

    def add_arguments(self, parser):
        parser.add_argument(
            "root_folder",
            type=str,
            help="Path to the root folder containing the metadata.json file and related files",
        )

    def handle(self, *args, **options):
        root_folder = options["root_folder"]

        # Check if the root folder exists
        if not os.path.exists(root_folder):
            self.stderr.write(f"Error: Root folder '{root_folder}' does not exist.")
            return

        # Check if metadata.json exists in the root folder
        metadata_file = os.path.join(root_folder, "metadata.json")
        if not os.path.exists(metadata_file):
            self.stderr.write(f"Error: metadata.json not found in '{root_folder}'.")
            return

        # Load the JSON file
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                problems_metadata = json.load(f)
        except json.JSONDecodeError as e:
            self.stderr.write(
                f"Error: Failed to parse JSON file '{metadata_file}': {e}"
            )
            return

        # Process each problem
        for problem_dict in problems_metadata:
            try:
                # Resolve the relative paths to absolute paths
                statement_file_path = os.path.join(
                    root_folder, problem_dict["statement_file"]
                )
                test_file_path = os.path.join(root_folder, problem_dict["test_file"])

                # Ensure the statement file and test file exist
                if not os.path.exists(statement_file_path):
                    self.stderr.write(
                        f"Error: Statement file '{statement_file_path}' does not exist."
                    )
                    continue

                if not os.path.exists(test_file_path):
                    self.stderr.write(
                        f"Error: Test file '{test_file_path}' does not exist."
                    )
                    continue

                # Read statement file content
                with open(statement_file_path, "r", encoding="utf-8") as f:
                    statement_content = f.read()

                # Create the Problem instance
                problem = Problem.objects.create(
                    code=problem_dict["problem_code"],
                    name=problem_dict["problem_name"],
                    description=statement_content,
                    time_limit=float(problem_dict["time_limit"]),
                    memory_limit=int(problem_dict["memory_limit"])
                    * 1024,  # Convert MB to KB
                    is_public=False,
                    group=ProblemGroup.objects.get(id=int(problem_dict["group_id"])),
                    points=problem_dict.get("points", 1),
                    date=now(),
                )

                # Set the author of the problem (assuming a Profile for the author exists)
                author_profile = Profile.objects.filter(
                    user__username=problem_dict["problem_author"]
                ).first()
                if author_profile:
                    problem.authors.add(author_profile)

                # Create the ProblemData instance
                problem_data = ProblemData.objects.create(
                    problem=problem,
                    fileio_input=problem_dict["input_file"],
                    fileio_output=problem_dict["output_file"],
                )

                files = []

                # Upload and process the test file
                with open(test_file_path, "rb") as test_file:
                    problem_data.zipfile.save(
                        os.path.basename(test_file_path), File(test_file), save=True
                    )

                    # Process zip file contents
                    with ZipFile(test_file_path, "r") as zip_ref:
                        # Get all file names in the zip
                        all_files = zip_ref.namelist()

                        # Find input and output files
                        input_files = sorted(
                            [f for f in all_files if f.lower().endswith(".inp")]
                        )
                        output_files = sorted(
                            [f for f in all_files if f.lower().endswith(".out")]
                        )

                        # Create test cases for each input-output pair
                        for order, (input_file, output_file) in enumerate(
                            zip(input_files, output_files)
                        ):
                            ProblemTestCase.objects.create(
                                dataset=problem,
                                order=order,
                                type="C",
                                input_file=input_file,
                                output_file=output_file,
                                points=1,
                                is_pretest=False,
                            )
                            files.extend([input_file, output_file])

                # Generate problem data
                ProblemDataCompiler.generate(
                    problem, problem_data, problem.cases.order_by("order"), files
                )

                # Output success message
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Successfully created problem '{problem.name}' with code '{problem.code}'."
                    )
                )

            except Exception as e:
                self.stderr.write(
                    f"Error creating problem '{problem_dict['problem_name']}': {e}"
                )
