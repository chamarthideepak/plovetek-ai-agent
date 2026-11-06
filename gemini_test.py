import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types


folder = Path(__file__).resolve().parent
load_dotenv(folder / ".env")

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise SystemExit("Add GEMINI_API_KEY to .env beside this file.")

try:
    with genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=60000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    ) as client:
        print("Requesting a sample draft from Gemini...")

        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=(
                "Write a short professional reply for Plovetek. "
                "A customer says: 'Please send me your pricing details.' "
                "Services and prices have not been provided. "
                "Ask the customer to describe their requirements so "
                "the owner can prepare a quotation. "
                "Do not invent prices, services, or commitments. "
                "Return only the email draft."
            ),
        )

        if response.text:
            print("\nAI-generated draft:\n")
            print(response.text)
        else:
            print("Gemini returned no draft.")

except errors.APIError as error:
    print(f"\nGemini API error {error.code}: {error.message}")
    if error.code == 503:
        print("Google is temporarily unavailable. Wait a minute and retry.")
    elif error.code == 429:
        print("Check the project's free-tier quota in Google AI Studio.")

except httpx.TimeoutException:
    print("\nThe request timed out. Wait a minute and retry.")

except KeyboardInterrupt:
    print("\nTest cancelled.")