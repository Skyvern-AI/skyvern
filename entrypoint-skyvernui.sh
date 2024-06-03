
#!/bin/bash

set -e

# setting api key
export VITE_SKYVERN_API_KEY=$(sed -n 's/.*cred\s*=\s*"\([^"]*\)".*/\1/p' .streamlit/secrets.toml)

npm run start


