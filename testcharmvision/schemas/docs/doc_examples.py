TASK_PROMPT_EXAMPLES = [
    "Find the top 3 posts on Hacker News.",
    'Go to google finance, extract the "AAPL" stock price for me with the date.',
]

TASK_URL_EXAMPLES = [
    "https://www.hackernews.com",
    "https://www.google.com/finance",
]

ERROR_CODE_MAPPING_EXAMPLES = [
    {"login_failed": "The login credentials are incorrect or the account is locked"},
    {"maintenance_mode": "The website is down for maintenance"},
]

TOTP_IDENTIFIER_EXAMPLES = [
    "john.doe@example.com",
    "4155555555",
    "user_123",
]

TOTP_URL_EXAMPLES = [
    "https://my-totp-service.com/totp",
]

BROWSER_SESSION_ID_EXAMPLES = [
    "pbs_123",
]
