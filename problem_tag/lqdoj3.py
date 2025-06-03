import asyncio
import fastapi_poe as fp
from dotenv import load_dotenv
import os
import time
import ast  # For safely parsing the response instead of using eval()

# Load the API key from the .env file
load_dotenv()
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise Exception("API_KEY not found in .env file. Please add it to your .env file.")


# Function to load tags from the `listtagFull.txt` file
def load_tags(tagfile):
    try:
        with open(tagfile, "r", encoding="utf-8") as file:
            tags = eval(file.read())  # Parse tags as a Python list
            if not isinstance(tags, list):
                raise ValueError("The file does not contain a valid list of tags.")
            return tags
    except Exception as e:
        print(f"Error reading {tagfile}: {e}")
        return []


# Function to load coding problems (code name and description)
def load_problems(codes_file, descriptions_folder):
    try:
        with open(codes_file, "r", encoding="utf-8") as file:
            code_names = eval(file.read())  # Parse as a Python list
            if not isinstance(code_names, list):
                raise ValueError(
                    "The file does not contain a valid list of code names."
                )

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
async def predict_difficulty_and_tags(api_key, description, tags, timeout=30):
    try:
        # Join all tags into a single string to include in the prompt
        tags_str = ", ".join(tags)

        # Request the analysis with strict formatting
        response = ""

        async def response_generator():
            nonlocal response
            async for partial in fp.get_bot_response(
                messages=[
                    fp.ProtocolMessage(
                        role="user",
                        content=(
                            f"Analyze the following coding problem and predict its difficulty score (like Codeforces rating) "
                            f"and associated tags. The output tags must strictly belong to the following list: "
                            f"[{tags_str}]. Provide your response in the exact format: (point, [tag1, tag2, tag3, ...]). "
                            f"No additional text should be included.\n\n"
                            f"**Problem Description**:\n{description}\n"
                        ),
                        timestamp=int(time.time()),
                    )
                ],
                bot_name="GPT-4o",  # Replace with your desired bot name
                api_key=api_key,
            ):
                response += partial.text

        await asyncio.wait_for(response_generator(), timeout=timeout)
        return response.strip()
    except asyncio.TimeoutError:
        print("\nResponse took too long. Skipping to the next problem.")
        return "(None, [Error])"
    except Exception as e:
        print(f"\nError during API call: {e}")
        return "(None, [Error])"


# Main function to process tags and problems
async def main():
    TAG_FILE = "listtagFull.txt"
    CODES_FILE = "listcode.txt"
    DESCRIPTIONS_FOLDER = "description"
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
        print(f"Processing problem: {code_name}")
        response = await predict_difficulty_and_tags(
            API_KEY, description, tags, timeout=30
        )
        if response:
            # Add the code name to the tuple
            try:
                # Use `ast.literal_eval` to safely parse the response
                point_and_tags = ast.literal_eval(response)
                if isinstance(point_and_tags, tuple) and len(point_and_tags) == 2:
                    point, tag_list = point_and_tags
                    if isinstance(point, int) and isinstance(tag_list, list):
                        results.append((code_name, point, tag_list))
                    else:
                        results.append((code_name, None, ["Invalid response"]))
                else:
                    results.append((code_name, None, ["Invalid response"]))
            except Exception as e:
                print(f"Error parsing response for {code_name}: {e}")
                results.append((code_name, None, ["Error"]))
        else:
            results.append((code_name, None, ["Error"]))

    # Save the results to an output file
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
            for result in results:
                file.write(f"{result}\n")
        print("\nFinal Results:")
        for result in results:
            # Print the final results to the screen
            print(result)
    except Exception as e:
        print(f"Error writing to {OUTPUT_FILE}: {e}")


# Run the main function
if __name__ == "__main__":
    asyncio.run(main())
