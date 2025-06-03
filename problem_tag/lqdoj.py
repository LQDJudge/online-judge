import asyncio
import fastapi_poe as fp
from dotenv import load_dotenv
import os
import time

# Load the API key from the .env file
load_dotenv()
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise Exception("API_KEY not found in .env file. Please add it to your .env file.")


# Function to load tags from the listtag.txt file
def load_tags(file_path):
    try:
        with open(file_path, "r") as file:
            data = file.read()
            # Evaluate the list format in the file
            tags = eval(data)
            if not isinstance(tags, list):
                raise ValueError("The file does not contain a valid list of tags.")
            return tags
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return []


# Function to send a tag question to the Poe API and get a response
async def get_response(api_key, messages, timeout=30):
    try:
        response = ""

        async def response_generator():
            nonlocal response
            async for partial in fp.get_bot_response(
                messages=messages,
                bot_name="Claude-3.5-Sonnet",  # You can change this to the bot you want to use
                api_key=api_key,
            ):
                response += partial.text
                print(partial.text, end="", flush=True)

        await asyncio.wait_for(response_generator(), timeout=timeout)
        print()  # New line after response
        return response
    except asyncio.TimeoutError:
        print("\nResponse took too long. Please try a different question.")
    except Exception as e:
        print(f"\nError during API call: {e}")
    return None


# Main function to process tags and send requests to the Poe API
async def main():
    tags_file = "listtag.txt"
    output_file = "output_responses.txt"

    # Load the tags
    tags = load_tags(tags_file)
    if not tags:
        print("No tags to process. Exiting.")
        return

    print(f"Loaded {len(tags)} tags from {tags_file}.")

    responses = {}
    for tag in tags:
        print(f"\nProcessing tag: {tag}")
        messages = [
            fp.ProtocolMessage(
                role="user",
                content=f"What are some advantages of {tag} for a beginner learning to code?",
                timestamp=int(time.time()),
            )
        ]
        print("Bot Response: ", end="")
        response = await get_response(API_KEY, messages, timeout=30)
        if response:
            responses[tag] = response
        else:
            responses[tag] = "No response or error occurred."

    # Save the responses to an output file
    try:
        with open(output_file, "w", encoding="utf-8") as file:
            for tag, response in responses.items():
                file.write(f"Tag: {tag}\n")
                file.write(f"Response: {response}\n")
                file.write("-" * 50 + "\n")
        print(f"\nResponses saved to {output_file}.")
    except Exception as e:
        print(f"Error writing to {output_file}: {e}")


# Run the main function
asyncio.run(main())
