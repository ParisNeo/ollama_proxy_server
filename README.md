# Ollama Proxy Fortress: Your Personal AI Security Gateway 🛡️

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)
![Built with](https://img.shields.io/badge/Built%20with-FastAPI-brightgreen)
![Release](https://img.shields.io/badge/release-v8.0.0-blue)
[![GitHub stars](https://img.shields.io/github/stars/ParisNeo/ollama_proxy_server.svg?style=social&label=Star)](https://github.com/ParisNeo/ollama_proxy_server/stargazers/)

Stop exposing your local AI to the world. **Ollama Proxy Fortress** is the ultimate security and management layer for your Ollama instances, designed to be set up in **60 seconds** by anyone, on any operating system.

Whether you're a developer, a researcher, or just an AI enthusiast running models on your personal machine, this tool transforms your setup from a vulnerable open port into a managed, secure, and powerful AI hub.

---

## The Threat: Why Your PC Could Be at Risk

Last year, a critical vulnerability named **"Probllama" (CVE-2024-37032)** was discovered in Ollama. This wasn't a minor bug; it was a **Remote Code Execution (RCE)** vulnerability.

**What does that mean in plain English?** It means an attacker from anywhere on the internet could have sent a malicious request to your Ollama server and potentially:
*   💻 **Take full control of your computer.**
*   훔 **Steal your personal files, documents, and private keys.**
*   🗑️ **Delete your data or install ransomware.**
*   🤫 **Use your computer for malicious activities without your knowledge.**

While the core Ollama team impressively patched this vulnerability, the incident highlighted a crucial need for a dedicated security layer. Running an AI model should not mean opening a backdoor to your digital life.

### So, Why Do You Still Need This?

Ollama Proxy Fortress is **more than just a patch**. It's a permanent solution that offers layers of security and powerful features that core Ollama doesn't provide:

*   🛡️ **Ironclad Security Gateway:** Enforce API key authentication for every single request. Add rate limiting to prevent abuse and filter access by IP address.
*   👤 **Centralized User Management:** Create different "users" for your different apps or family members, each with their own unique, revocable API keys.
*   🌐 **Multi-Server Management & Federation:** Centrally manage all your Ollama backend servers. The proxy unifies them, allowing you to see and use models from all your servers as if they were one.
*   📊 **Usage Insights:** A beautiful admin dashboard shows you exactly which keys are being used and how often.
*   🚀 **Effortless 1-Click Setup:** No Docker, no `pip install`, no command-line wizardry required. Just download and run a single script.

---

## Get Started in 60 Seconds (Yes, Really!)

This is the easiest way to secure your AI setup, period.

### 1. Download the Project

Download the source code from the repository, either by using `git` or by downloading the ZIP file and extracting it.

```bash
git clone https://github.com/ParisNeo/ollama_proxy_server.git
cd ollama_proxy_server
```

### 2. Run the Installer

Based on your operating system, run the appropriate script. The first time you run it, it will guide you through a simple setup wizard. Every time after that, it will just start the server.

**On Windows:**
Simply double-click `run_windows.bat`.

**On macOS or Linux:**
Open your terminal, navigate to the project folder, and run:```bash
chmod +x run.sh
./run.sh
```

**That's it!** The server is now running. To stop it, just close the terminal window or press `Ctrl+C`.

---

## Visual Walkthrough & Features

### Step 1: Secure Admin Login

Once the server is running, go to the admin panel (e.g., `http://127.0.0.1:8080/admin`). Log in with the secure credentials you created during setup.

![Secure Admin Login Page](assets/login.png)

### Step 2: Manage Users and API Keys

The main dashboard is your central hub for creating virtual users. This allows you to generate and organize unique API keys for different applications or individuals. Click "Manage" to create, view, and revoke keys for any user.

![User Management Dashboard](assets/dashboard.png)

### Step 3: Centrally Manage Ollama Servers

The "Server Management" page lets you add and remove all your backend Ollama instances. The proxy will load-balance requests across all active servers, and the `/api/tags` endpoint will show a federated list of models from all of them.

![Ollama Server Management](assets/server_management.png)

### Step 4: Monitor Usage Statistics

Get a clear overview of your API usage. The "Usage Stats" page shows a breakdown of total requests per user and per API key, helping you understand which services are most active.

![API Usage Statistics](assets/stats.png)

### Step 5: Make Secure API Calls

Configure your applications to use the proxy URL and provide the API key as a Bearer token in the `Authorization` header. Your underlying Ollama server is now completely shielded from direct access.

```bash
curl http://127.0.0.1:8080/api/generate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer op_prefix_secret" \
  -d '{
    "model": "llama3",
    "prompt": "Why is the sky blue?"
  }'
```

### Step 6: Get Help When You Need It

The built-in "Help & Credits" page provides a quick-start guide and acknowledges the open-source projects that make this application possible.

![Help and Credits Page](assets/help.png)

---

## For the Power Users: Docker Deployment

If you prefer a container-based workflow, we've got you covered.

**1. Build the Docker image:**
```bash
docker build -t ollama-proxy-server .
```

**2. Run the container:**
Create a `.env` file on your host machine, then run:
```bash
docker run -d --name ollama-proxy \
  -p 8080:8080 \
  --env-file ./.env \
  -v ./ollama_proxy.db:/home/app/ollama_proxy.db \
  ollama-proxy-server
```

---

## Credits and Acknowledgements

This application was developed with passion by the open-source community and stands on the shoulders of giants. A special thank you to:

*   **Saifeddine ALOUI (ParisNeo)** for creating and maintaining this project.
*   The teams behind **FastAPI**, **SQLAlchemy**, **Alembic**, **Jinja2**, and **Tailwind CSS** for their incredible open-source tools.

Visit the project on [GitHub](https://github.com/ParisNeo/ollama_proxy_server) to contribute, report issues, or star the repository!

---

## License

This project is licensed under the Apache License 2.0. Feel free to use, modify, and distribute.