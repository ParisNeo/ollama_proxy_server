# Ollama Proxy Fortress: Your Personal AI Security Gateway 🛡️

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)
![Built with](https://img.shields.io/badge/Built%20with-FastAPI-brightgreen)
![Release](https://img.shields.io/badge/release-v9.0.0-blue)
[![GitHub stars](https://img.shields.io/github/stars/ParisNeo/ollama_proxy_server.svg?style=social&label=Star)](https://github.com/ParisNeo/ollama_proxy_server/stargazers/)

Stop exposing your local AI to the world. **Ollama Proxy Fortress** is the ultimate security and management layer for your Ollama instances, designed to be set up in **60 seconds** by anyone, on any operating system.

Whether you're a developer, a researcher, or just an AI enthusiast, this tool transforms your vulnerable open port into a managed, secure, and **deeply customizable** AI command center.

---

## The Threat: Why Your PC Could Be at Risk

A critical vulnerability named **"Probllama" (CVE-2024-37032)** was discovered in Ollama, allowing Remote Code Execution (RCE). In plain English, an attacker could have sent a malicious request to your Ollama server and **taken full control of your computer**—stealing files, installing ransomware, or using your machine for malicious activities.

While the core team patched this, the incident highlighted a crucial need for a dedicated security layer. Running an AI model should not mean opening a backdoor to your digital life.

### So, Why Do You Still Need This?

Ollama Proxy Fortress is **more than just a patch**. It's a permanent solution that unleashes a suite of powerful, enterprise-grade features that core Ollama doesn't provide:

*   ✨ **Centralized Model Management:** Pull, update, and delete models on any of your connected Ollama servers directly from the proxy's web UI. No more terminal commands or switching between machines.

*   🛡️ **Rock-Solid Security:**
    *   **Endpoint Blocking:** Prevent API key holders from accessing sensitive endpoints like `pull`, `delete`, and `create` to protect your servers from abuse.
    *   **API Key Authentication:** Eliminate anonymous access entirely.
    *   **One-Click HTTPS/SSL:** Encrypt all traffic with easy certificate uploads or path-based configuration.
    *   **IP Filtering:** Create granular allow/deny lists to control exactly which machines can connect.
    *   **Rate Limiting & Brute-Force Protection:** Prevent abuse and secure your admin login (powered by Redis).

*   🚀 **High-Performance Engine:**
    *   **Intelligent Load Balancing:** Distribute requests across multiple Ollama servers for maximum speed and high availability.
    *   **Smart Model Routing:** Automatically sends requests only to servers that have the specific model available, preventing failed requests and saving compute resources.
    *   **Automatic Retries:** The proxy resiliently handles temporary server hiccups with an exponential backoff strategy, making your AI services more reliable.

*   🧪 **Model Playgrounds & Benchmarking:**
    *   **Interactive Chat Playground:** Go beyond simple API calls. Chat with any model in a familiar interface that supports streaming, multi-modal inputs (paste images directly!), and full conversation history management (import/export).
    *   **Advanced Embedding Playground:** A powerful tool for data scientists and developers. Visualize how different embedding models "understand" language by plotting concepts in a 2D space. Use pre-built benchmarks or create your own to compare model performance side-by-side.

*   📊 **Mission Control Dashboard:**
    *   Go beyond `ollama ps`. Get a real-time, auto-updating view of your proxy's health (CPU, Memory, Disk), see all active models across all servers, monitor the **live health of your load balancer**, and watch API rate-limit queues fill and reset in real-time.

*   📈 **Comprehensive Analytics Suite:**
    *   Don't just guess your usage—know it. Dive into beautiful, interactive charts for daily and hourly requests, model popularity, and server load.
    *   With a single click, drill down into **per-user analytics** to understand individual usage patterns. All data is exportable to CSV or PNG.

*   🎨 **Radical Theming Engine:**
    *   Why should your tools be boring? Choose from over a dozen stunning UI themes to match your personal aesthetic. Whether you prefer a sleek **Material Design**, a futuristic **Cyberpunk** neon glow, a retro **CRT Terminal**, or a stark **Brutalist** look, you can make the interface truly yours.

*   👤 **Granular User & API Key Management:**
    *   Effortlessly create and manage users. The sortable user table gives you at-a-glance stats on key counts, total requests, and last activity.
    *   From there, manage individual API keys with per-key rate limits, and temporarily disable or re-enable keys on the fly.

*   🌐 **Multi-Server Management & Federation:**
    *   Centrally manage all your Ollama backend servers. The proxy load-balances requests and provides a unified, federated view of all available models from all your instances combined.

*   ✨ **Effortless 1-Click Setup:**
    *   No Docker, no `pip install`, no command-line wizardry required. Just download and run a single script.

---

## 🛡️ Harden Your Defenses: Endpoint Blocking

Giving every user an API key shouldn't mean giving them the keys to the kingdom. By default, **Ollama Proxy Fortress blocks access to dangerous and resource-intensive API endpoints** for all API key holders.

-   **Prevent Denial-of-Service:** Stop users from triggering massive model downloads (`/api/pull`) that can saturate your network and fill your disk.
-   **Protect Your Models:** Prevent API users from deleting (`/api/delete`), copying (`/api/copy`), or creating (`/api/create`) models on your backend servers.
-   **Full Admin Control:** As an administrator, you can still perform all these actions securely through the web UI's **Model Management** page.
-   **Customizable:** You have full control to change which endpoints are blocked via the **Settings -> Endpoint Security** menu.

---

## 🔒 Encrypt Everything with One-Click HTTPS/SSL

Securing your AI traffic is now dead simple. In the **Settings -> HTTPS/SSL** menu, you have two easy options:

1.  **Upload & Go (Easiest):** Simply upload your `key.pem` and `cert.pem` files directly through the UI. The server handles the rest.
2.  **Path-Based:** If your certificates are already on the server (e.g., managed by Certbot), just provide the full file paths.

A server restart is required to apply changes, ensuring your connection is fully encrypted and secure from eavesdropping.

---

## Get Started in 60 Seconds (Yes, Really!)

### 1. Download the Project

Download the source code from the repository, either by using `git` or by downloading the ZIP file and extracting it.

```bash
git clone https://github.com/ParisNeo/ollama_proxy_server.git
cd ollama_proxy_server
```

### 2. Run the Installer

Based on your operating system, run the appropriate script. The first time you run it, it will guide you through a simple setup wizard.

**On Windows:**
Simply double-click `run_windows.bat`.

**On macOS or Linux:**
Open your terminal, navigate to the project folder, and run:
```bash
chmod +x run.sh
./run.sh
```

**That's it!** The server is now running. To stop it, just close the terminal window or press `Ctrl+C`.

---

## Visual Showcase

### Step 1: Secure Admin Login

Log in with the secure credentials you created during setup.

![Secure Admin Login Page](assets/login.png)

### Step 2: The Command Center Dashboard

Your new mission control. Instantly see system health, active models, server status, and live rate-limit queues, all updating automatically.

![Dashboard](assets/DashBoard.gif)

### Step 3: Manage Your Servers & Models

No more SSH or terminal juggling. Add all your Ollama instances, then pull, update, and delete models on any server with a few clicks.

![Server Management](assets/server_management.png)

### Step 4: Choose Your Look: The Theming Engine

Navigate to the Settings page and instantly transform the entire UI. Pick a style that matches your mood or your desktop setup.

![Theming](assets/theming.gif)

### Step 5: Manage Users & Drill Down into Analytics

The User Management page gives you a sortable, high-level overview. From here, click "View Usage" to dive into a dedicated analytics page for any specific user.

![User edit](assets/user_edit.gif)

### Step 6: Test & Benchmark in the Playgrounds

Use the built-in playgrounds to evaluate your models. The **Chat Playground** provides a familiar UI to test conversational models with streaming and image support. The **Embedding Playground** lets you visualize and benchmark how different models understand semantic relationships using powerful 2D plots.

### Step 7: Master Your Analytics

The main "Usage Stats" page and the per-user pages give you a beautiful, exportable overview of exactly how your models are being used.

![API Usage Statistics](assets/stats.png)

### Step 8: Get Help When You Need It

The built-in Help page is now a rich document with a sticky table of contents that tracks your scroll position, making it effortless to find the information you need.

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
  -v ./.ssl:/home/app/.ssl \
  -v ./app/static/uploads:/home/app/app/static/uploads \
  ollama-proxy-server
```
*Note the extra volume mounts for the database, SSL files, and user uploads to persist data outside the container.*

---

## Resetting Your Installation (Troubleshooting)

> **WARNING: IRREVERSIBLE ACTION**
>
> The reset scripts are for troubleshooting or starting over completely. They will **PERMANENTLY DELETE** your database, configuration, and Python environment.

If you encounter critical errors or wish to perform a completely fresh installation, use the provided reset scripts.

**On Windows:**
Double-click the `reset.bat` file.

**On macOS or Linux:**
```bash
chmod +x reset.sh
./reset.sh
```

---

## Credits and Acknowledgements

This application was developed with passion by the open-source community. A special thank you to:

*   **ParisNeo** for creating and maintaining this project.
*   All contributors who have helped find and fix bugs.
*   The teams behind **FastAPI**, **SQLAlchemy**, **Jinja2**, **Chart.js**, and **Tailwind CSS**.

Visit the project on [GitHub](https://github.com/ParisNeo/ollama_proxy_server) to contribute, report issues, or star the repository!

---

## License

This project is licensed under the Apache License 2.0. Feel free to use, modify, and distribute.
