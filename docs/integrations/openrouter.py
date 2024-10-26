import os

import requests


def get_openrouter_response() -> None:
    url = "https://openrouter.ai/api/v1/chat/completions"

    # Create headers and payload
    headers = {
        "Authorization": f"Bearer {os.getenv('openrouter_api_key')}",
        "HTTP-Referer": os.getenv("your_site_url"),
        "X-Title": os.getenv("your_site_name"),
        "Content-Type": "application/json",
    }

    payload = {"model": "OPENROUTER_MODEL", "messages": [{"role": "user", "content": "YOUR QUESTION HERE"}]}

    print("Sending request...")

    # Send request and capture response
    response = requests.post(url, headers=headers, json=payload)

    # Print status code and response content
    print("Response Content:", response.text)

    # Handle the response
    if response.ok:
        try:
            print("Response JSON:", response.json())
        except ValueError:
            print("Response is not in JSON format.")
    else:
        print(f"Error: {response.status_code}\n{response.text}")


get_openrouter_response()
