import asyncio
import fastapi_poe as fp
from dotenv import load_dotenv
import os
import time

# Load the API key from the .env file
load_dotenv()  # Loads environment variables from `.env` file
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise Exception("API_KEY not found in .env file. Please add it to your .env file.")


# Function to load tags from the `listtag.txt` file
def load_tags(tagfile):
    try:
        with open(tagfile, "r", encoding="utf-8") as file:
            data = file.read()
            tags = eval(data)  # Expecting a Python list format
            if not isinstance(tags, list):
                raise ValueError("The file does not contain a valid list of tags.")
            return tags
    except Exception as e:
        print(f"Error reading {tagfile}: {e}")
        return []


# Function to load coding problems (code name and description)
def load_problems(codes_file, descriptions_folder):
    try:
        # Read the list of code names from `listcode.txt`
        with open(codes_file, "r", encoding="utf-8") as file:
            code_names = eval(file.read())
            if not isinstance(code_names, list):
                raise ValueError(
                    "The file does not contain a valid list of code names."
                )

        # Read the descriptions for each code name from the `description` folder
        problems = []
        for code_name in code_names:
            description_file = os.path.join(descriptions_folder, f"{code_name}.txt")
            try:
                with open(description_file, "r", encoding="utf-8") as desc_file:
                    description = desc_file.read()
                    problems.append((code_name, description))
            except FileNotFoundError:
                print(f"Warning: Description file for '{code_name}' not found.")

        return problems
    except Exception as e:
        print(f"Error reading problems: {e}")
        return []


# Function to query Poe API for difficulty and tags
async def predict_difficulty_and_tags(api_key, code_name, description, timeout=30):
    try:
        response = ""

        async def response_generator():
            nonlocal response
            async for partial in fp.get_bot_response(
                messages=[
                    fp.ProtocolMessage(
                        role="user",
                        content=(
                            f"Analyze the following coding problem and predict its difficulty score (like Codeforces rating) "
                            f"and associated tags (from the given list of tags).\n\n"
                            f"**Code Name**: {code_name}\n\n"
                            f"**Problem Description**:\n{description}\n\n"
                            f"Respond in the format: (difficulty, [tags])."
                        ),
                        timestamp=int(time.time()),
                    )
                ],
                bot_name="Claude-3.5-Sonnet",  # Replace with your desired bot name
                api_key=api_key,
            ):
                response += partial.text
                print(partial.text, end="", flush=True)

        await asyncio.wait_for(response_generator(), timeout=timeout)
        print()  # New line after response
        return response
    except asyncio.TimeoutError:
        print(
            f"\nResponse for '{code_name}' took too long. Skipping to the next problem."
        )
    except Exception as e:
        print(f"\nError during API call for '{code_name}': {e}")
    return None


# Main function to process tags and problems
async def main():
    # File paths based on your system structure
    TAG_FILE = "listtag.txt"
    CODES_FILE = "listcode.txt"
    DESCRIPTIONS_FOLDER = "description"  # Folder containing problem descriptions
    OUTPUT_FILE = "predictions.txt"

    # Load tags
    tags = load_tags(TAG_FILE)
    if not tags:
        print("No tags loaded. Exiting.")
        return
    print(f"Loaded {len(tags)} tags from {TAG_FILE}.")

    # Load problems
    problems = load_problems(CODES_FILE, DESCRIPTIONS_FOLDER)
    if not problems:
        print("No problems loaded. Exiting.")
        return
    print(
        f"Loaded {len(problems)} problems from {CODES_FILE} and {DESCRIPTIONS_FOLDER}."
    )

    results = []
    for code_name, description in problems:
        print(f"\nProcessing problem: {code_name}")
        response = await predict_difficulty_and_tags(
            API_KEY, code_name, description, timeout=30
        )
        if response:
            results.append((code_name, response))
        else:
            results.append((code_name, "No response or error occurred."))

    # Save the results to an output file
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
            for code_name, result in results:
                file.write(f"Code Name: {code_name}\n")
                file.write(f"Prediction: {result}\n")
                file.write("-" * 50 + "\n")
        print(f"\nResults saved to {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error writing to {OUTPUT_FILE}: {e}")


# Run the main function
if __name__ == "__main__":
    asyncio.run(main())
