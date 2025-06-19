# Skyvern-Coolify

This repository is a **fork** of the upstream [Skyvern](https://github.com/skyvern-ai/skyvern) project, for seamless(ish) deployment on [Coolify](https://coolify.io/). 

---

## 🚀 About This Fork

This fork introduces several key changes to optimize for Coolify deployments. It *should* sync unmodified files with the upstream repo nightly. Very much a work-in-progress.

### Core Differences with Upstream

1. **Artifact Server as a Separate Container**  
   - The `docker-compose.yaml` here splits the artifact server into its own dedicated container (`artifact-server`).  
   - This improves scalability, reliability, and makes artifact management more modular and robust for distributed cloud environments.

2. **Dedicated Artifact Endpoint Handler**  
   - The repository adds a new handler for the artifact server endpoint, tailored for production artifact delivery and separation of concerns.

3. **Coolify-Friendly VITE Domain Variables**  
   - All relevant `VITE_*` domain variables are exposed as environment variables, making it easy to adjust domains and endpoints via the Coolify UI.
   - The main UI and API endpoints are now fully customizable for your deployment, supporting custom FQDNs and subdomains.

4. **Environment Variables for Coolify**  
   - The deployment is designed for configuration via the Coolify UI.  
   - All sensitive and domain-specific environment variables (like `COOLIFY_FQDN`, `VITE_API_BASE_URL`, `VITE_WSS_BASE_URL`, `VITE_ARTIFACT_API_BASE_URL`, etc.) are configurable in Coolify.
   - No `.env` files are required or recommended; all configs are managed via Coolify's secrets and env var interface.

---

## 🗝️ **Environment Variables**

Set the following environment variables in Coolify for a successful deployment. You can edit these in the Coolify UI under "Environment Variables" for each service.

**Core Required Variables:**

- `COOLIFY_FQDN` — Your main deployment domain (e.g., `mydomain.com`)
- `VITE_API_BASE_URL` — API endpoint, e.g., `http://api.${COOLIFY_FQDN}/api/v1`
- `VITE_WSS_BASE_URL` — WebSocket endpoint, e.g., `wss://api.${COOLIFY_FQDN}/api/v1`
- `VITE_ARTIFACT_API_BASE_URL` — Artifact API endpoint, e.g., `http://artifact.${COOLIFY_FQDN}`
- `VITE_SKYVERN_API_KEY` — (Set after first deployment via Skyvern UI)
- Any LLM provider secrets needed (e.g., `OPENAI_API_KEY`, `GEMINI_API_KEY`, etc.)

**Other Optional Variables:**

- `ENABLE_OPENAI`, `ENABLE_GEMINI`, etc. — Toggle specific LLM providers
- `LLM_KEY` — Choose which model to use (e.g., `OPENAI_GPT4O`)

> **Note:** All variables can be set or overridden in the Coolify UI. No local `.env` is used.

---

## 🐳 **docker-compose.yaml Overview**

- **`skyvern`**: Main backend service, with persistent volumes for artifacts, logs, and more.  
- **`artifact-server`**: Serves artifacts from a dedicated container (see `skyvern-frontend/dockerfile.artifact`).  
- **`skyvern-ui`**: Frontend UI container, with `VITE_*` domain variables exposed for full Coolify integration.
- **`postgres`**: Database persistence.

---

## 🏗️ **Step-by-Step: Deploying on Coolify**


### 1. **Connect Repo in Coolify**

- Log in to your Coolify instance.
- Select or build the project where you want to deploy Skyvern, select "New Resource" then select the "Public Repository" application. Enter the url for this repo: "https://github.com/olsonbd/skyvern-coolify"
- For deployment type, select "Docker Compose"

### 4. **Set Environment Variables**

For each service (`skyvern`, `artifact-server`, `skyvern-ui`):

- In the Coolify UI, set the required environment variables:
  - `COOLIFY_FQDN` (e.g., `mydomain.com`)
  - All `VITE_*` variables as shown above
  - Any required LLM API keys or toggles -- see comments in docker-compose.yaml for environment variables and values correspsonding to your setup and add in the UI as needed. 
- Ensure ports are mapped as needed (defaults: 8001 for `skyvern`, 8081 for `skyvern-ui`, 9090 for `artifact-server`).

### 5. **Deploy**

- Click "Deploy" in Coolify.
- Coolify will build and start all containers as described in `docker-compose.yaml`.

### 6. **Post-Deploy: Configure API Key**

- After your first deployment, access the Skyvern UI at `https://your-coolify-domain/`.
- Generate or retrieve the `VITE_SKYVERN_API_KEY` from the UI.
- Add this key as an environment variable for the `skyvern-ui` service in Coolify and redeploy if necessary.

---

## 📝 **Summary of Customization**

- **Modular artifact server for production robustness**
- **All domain and API endpoints are dynamically settable via Coolify environment variables**
- **No local .env files required — all managed via PaaS UI**
- **Ready for advanced deployment setups with custom subdomains, reverse proxies, and secrets management**

---

## 🔗 **Links**

- [Original Skyvern Upstream](https://github.com/skyvern-ai/skyvern)
- [Coolify Documentation](https://coolify.io/docs)
- [This Fork on GitHub](https://github.com/olsonbd/skyvern-coolify)
- [Docker Compose Reference](https://docs.docker.com/compose/)

---

## 🤝 **Contributing & Support**

Please open Issues and Pull Requests for any improvements or fixes!

---

**Deploy powerful, vision-driven browser automations — now optimized for modern PaaS.**
