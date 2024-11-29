import os

import requests


# model testing
def test_openrouter_model():
    url = "https://openrouter.ai/api/v1/chat/completions"
    api_key = os.getenv("OPENROUTER_API_KEY") or ''

    # Set headers and payload for test
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "openai/gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "This is a test message for model verification."}],
    }

    # Send test request
    response = requests.post(url, headers=headers, json=payload)

    if response.ok:
        # Display a sample response if the model responds successfully
        response_data = response.json()
        print("Model test successful!")
        print("Response:", response_data)
    else:
        # If there’s an error, print the error message
        print(f"Model test failed with status code {response.status_code}.")
        print("Error details:", response.text)


# Call the testing function
test_openrouter_model()
