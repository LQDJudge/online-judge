import asyncio
import fastapi_poe as fp
from dotenv import load_dotenv
import os
import time
import ast  # For safely parsing the response instead of using eval()
import re  # For sanitizing the chatbot's response

# Load the API key from the .env file
load_dotenv()
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise Exception("API_KEY not found in .env file. Please add it to your .env file.")


async def get_response(api_key, messages, timeout=30):
    """
    Function to get a response from the Poe API using the given messages.
    """
    try:
        response = ""

        async def response_generator():
            nonlocal response
            async for partial in fp.get_bot_response(
                messages=messages, bot_name="GPT-4o", api_key=api_key
            ):
                response += partial.text
                print(partial.text, end="", flush=True)

        await asyncio.wait_for(response_generator(), timeout=timeout)
        print()  # New line after response
        return response
    except asyncio.TimeoutError:
        print("\nResponse took too long. Skipping this request.")
        return None
    except Exception as e:
        print(f"\nError during API call: {e}")
        return None


def sanitize_response(response):
    """
    Sanitize the chatbot response to ensure it is valid Python syntax.
    - Wrap improperly formatted tags in quotes.
    - Remove unexpected characters outside the expected format.
    """
    # Match the expected format: (point, [tag1, tag2, ...])
    match = re.match(r"\(\s*(\d+)\s*,\s*\[([^\]]*)\]\s*\)", response)
    if not match:
        raise ValueError("Response is not in the expected format.")

    # Extract the point and tags
    point = int(match.group(1))
    tags_raw = match.group(2)

    # Split tags and sanitize them (e.g., wrap in quotes if necessary)
    tags = [tag.strip() for tag in tags_raw.split(",")]
    sanitized_tags = []
    for tag in tags:
        # Wrap the tag in quotes if it's not already a valid Python string
        if not (tag.startswith("'") and tag.endswith("'")) and not (
            tag.startswith('"') and tag.endswith('"')
        ):
            sanitized_tags.append(f"'{tag}'")
        else:
            sanitized_tags.append(tag)

    # Reconstruct the sanitized tuple
    sanitized_response = f"({point}, [{', '.join(sanitized_tags)}])"
    return sanitized_response


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

    # Initialize messages with the system message to cache the tags
    tags_str = ", ".join(tags)
    messages = [
        fp.ProtocolMessage(
            role="system",
            content=(
                f"You are a coding assistant. You are given a set of tags: [{tags_str}]. "
                f"All responses must use only these tags when predicting difficulty and associated tags for coding problems."
            ),
            timestamp=int(time.time()),
        )
    ]

    results = []

    for code, description in problems:
        # Add the user message (problem description) to the chat flow
        user_message = (
            f"Analyze the following coding problem and predict its difficulty score (like Codeforces rating) "
            f"and associated tags. Respond only in the format: (point, [tag1, tag2, tag3, ...]).\n\n"
            f"{description}"
        )
        print(f"\nUser Request for {code}:\n{user_message}\n")
        messages.append(
            fp.ProtocolMessage(
                role="user", content=user_message, timestamp=int(time.time())
            )
        )

        # Get the response from the chatbot
        print("Chatbot Response: ", end="")
        response = await get_response(API_KEY, messages, timeout=30)

        if response:
            # Add the bot response to the chat flow
            messages.append(
                fp.ProtocolMessage(
                    role="bot", content=response, timestamp=int(time.time())
                )
            )

            # Parse and validate the response
            try:
                sanitized_response = sanitize_response(response.strip())
                parsed_response = ast.literal_eval(sanitized_response)
                if isinstance(parsed_response, tuple) and len(parsed_response) == 2:
                    point, tag_list = parsed_response
                    if isinstance(point, int) and isinstance(tag_list, list):
                        result = (code, point, tag_list)
                        results.append(result)
                        print(f"Formatted Result: {result}\n")
                    else:
                        raise ValueError("Invalid response format.")
                else:
                    raise ValueError("Response is not a valid tuple.")
            except Exception as e:
                print(f"Error parsing response for {code}: {e}")
                results.append((code, None, ["Error"]))
        else:
            results.append((code, None, ["Error"]))

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
