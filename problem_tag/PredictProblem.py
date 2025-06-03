import asyncio
import fastapi_poe as fp
from dotenv import load_dotenv
import os
import time
import ast  # For safely parsing the response instead of using eval()
import re  # For extracting and sanitizing the chatbot's response
from datetime import datetime  # For timestamp in logs

# Load the API key and sleep time from the .env file
load_dotenv()
API_KEY = os.getenv("API_KEY")
SLEEP_TIME = float(os.getenv("SLEEP_TIME", 2.5))  # Default sleep time is 2.5 seconds

if not API_KEY:
    raise Exception("API_KEY not found in .env file. Please add it to your .env file.")


async def get_response(api_key, messages, timeout=30):
    """
    Function to get a response from the Poe API using the given messages.
    Handles intermediate output like 'Thinking...' and extracts the final response.
    """
    try:
        response = ""

        async def response_generator():
            nonlocal response
            async for partial in fp.get_bot_response(
                messages=messages, bot_name="GPT-4o", api_key=api_key
            ):
                # Filter out intermediate "Thinking..." messages
                if "Thinking" not in partial.text:  # Ignore "Thinking" messages
                    response += partial.text
                print(partial.text, end="", flush=True)

        await asyncio.wait_for(response_generator(), timeout=timeout)
        print()  # New line after response
        return response.strip()
    except asyncio.TimeoutError:
        print("\nResponse took too long. Skipping this request.")
        return None
    except Exception as e:
        print(f"\nError during API call: {e}")
        return None


def sanitize_response(response):
    """
    Sanitize the chatbot response to ensure it is valid Python syntax.
    - Extract only the valid tuple from the response.
    - Ensure tags are properly quoted.
    """
    # Extract the valid tuple-like response using a regex
    match = re.search(r"\(\s*\d+\s*,\s*\[.*?\]\s*\)", response)
    if not match:
        raise ValueError("Response is not in the expected format.")

    # Extract the matched part
    sanitized_response = match.group(0)

    # Sanitize the tags inside the tuple
    sanitized_response = re.sub(
        r"(?<=\[)([^\[\]']+)(?=\])",  # Match unquoted tags inside brackets
        lambda m: ", ".join(
            f"'{tag.strip()}'" for tag in m.group(1).split(",")
        ),  # Quote each tag
        sanitized_response,
    )

    return sanitized_response


async def process_problem(api_key, code, description, tags, max_retries=5):
    """
    Process a single problem, retrying up to `max_retries` times if necessary.
    Log errors to a file if retries are exhausted.
    """
    ERROR_LOG_FILE = "log_errors.txt"
    tags_str = ", ".join(tags)
    messages = [
        fp.ProtocolMessage(
            role="system",
            content=(
                f"You are a coding assistant. You are given a set of tags: [{tags_str}]. "
                f"All responses must use only these tags when predicting difficulty and associated tags for coding problems."
            ),
            timestamp=int(time.time()),
        ),
        fp.ProtocolMessage(
            role="user",
            content=(
                f"Analyze the following coding problem and predict its difficulty score (like Codeforces rating) "
                f"and associated tags. Respond only in the format: (point, [tag1, tag2, tag3, ...]).\n\n"
                f"{description}"
            ),
            timestamp=int(time.time()),
        ),
    ]

    for attempt in range(max_retries):
        print(f"\nAttempt {attempt + 1} for problem {code}:")
        response = await get_response(api_key, messages, timeout=30)

        if response:
            try:
                # Sanitize and parse the final response
                sanitized_response = sanitize_response(response)
                print(f"Sanitized Response: {sanitized_response}")
                parsed_response = ast.literal_eval(sanitized_response)
                if isinstance(parsed_response, tuple) and len(parsed_response) == 2:
                    point, tag_list = parsed_response
                    if point is not None and tag_list != ["Error"]:
                        # Successfully parsed response
                        return (code, point, tag_list)
                    else:
                        raise ValueError("Response contains invalid data.")
                else:
                    raise ValueError("Response is not a valid tuple.")
            except Exception as e:
                print(f"Error parsing response for {code}: {e}")
        else:
            print(f"Failed to get a valid response for {code}.")

        # Log the error
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{datetime.now()}] Failed attempt {attempt + 1} for problem {code}: {response}\n"
            )

        # Delay before retrying
        print(f"Sleeping for {SLEEP_TIME} seconds before retrying...")
        await asyncio.sleep(SLEEP_TIME)

    # Return error if all retries fail
    return (code, None, ["Error"])


async def main():
    TAG_FILE = "listtagFull.txt"
    CODES_FILE = "listcode.txt"
    DESCRIPTIONS_FOLDER = "description"
    OUTPUT_FILE = "predictions.txt"

    # Load tags from file
    try:
        with open(TAG_FILE, "r", encoding="utf-8") as file:
            tags = eval(file.read())
            if not isinstance(tags, list):
                raise ValueError("Tag file must contain a valid list of tags.")
    except Exception as e:
        print(f"Error reading {TAG_FILE}: {e}")
        return
    print(f"Loaded {len(tags)} tags from {TAG_FILE}.")

    # Load problems from file
    try:
        with open(CODES_FILE, "r", encoding="utf-8") as file:
            codes = eval(file.read())
            if not isinstance(codes, list):
                raise ValueError("Code file must contain a valid list of codes.")
    except Exception as e:
        print(f"Error reading {CODES_FILE}: {e}")
        return

    problems = []
    for code in codes:
        description_file = os.path.join(DESCRIPTIONS_FOLDER, f"{code}.txt")
        try:
            with open(description_file, "r", encoding="utf-8") as desc_file:
                description = desc_file.read()
                problems.append((code, description))
        except FileNotFoundError:
            print(f"Warning: Description file for '{code}' not found.")

    if not problems:
        print("No problems loaded. Exiting.")
        return
    print(
        f"Loaded {len(problems)} problems from {CODES_FILE} and {DESCRIPTIONS_FOLDER}."
    )

    results = []
    for code, description in problems:
        result = await process_problem(API_KEY, code, description, tags)
        results.append(result)
        print(f"Sleeping for {SLEEP_TIME} seconds before the next request...")
        await asyncio.sleep(SLEEP_TIME)  # Delay before the next request

    # Write results to the output file
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
            for result in results:
                file.write(f"{result}\n")
        print(f"\nResults saved to {OUTPUT_FILE}.")
    except Exception as e:
        print(f"Error writing to {OUTPUT_FILE}: {e}")


# Run the main function
if __name__ == "__main__":
    asyncio.run(main())
