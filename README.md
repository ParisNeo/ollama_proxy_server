# Ollama Proxy Fortress: Your Personal AI Security Gateway 🛡️

Stop exposing your local AI to the world. **Ollama Proxy Fortress** is the ultimate security and management layer for your Ollama instances, designed to be set up in **60 seconds** by anyone, on any operating system.

![GIF placeholder: A quick tour of the Admin Dashboard, showing user creation and key generation.](https://your-gif-hosting.com/admin_tour.gif)

Whether you're a developer, a researcher, or just an AI enthusiast running models on your personal machine, this tool transforms your setup from a vulnerable open port into a managed, secure, and powerful AI hub.

---

## The Threat: Why Your PC Could Be at Risk

Last year, a critical vulnerability named **"Probllama" (CVE-2024-37032)** was discovered in Ollama. This wasn't a minor bug; it was a **Remote Code Execution (RCE)** vulnerability.

**What does that mean in plain English?** It means an attacker from anywhere on the internet could have sent a malicious request to your Ollama server and potentially:
*   💻 **Take full control of your computer.**
*   훔 **Steal your personal files, documents, and private keys.**
*   🗑️ **Delete your data or install ransomware.**
*   🤫 **Use your computer for malicious activities without your knowledge.**

While the core Ollama team impressively patched this vulnerability in version `0.1.34`, the incident highlighted a crucial need for a dedicated security layer. Running an AI model should not mean opening a backdoor to your digital life.

### So, Why Do You Still Need This?

Ollama Proxy Fortress is **more than just a patch**. It's a permanent solution that offers layers of security and powerful features that core Ollama doesn't provide:

*   🛡️ **Ironclad Security Gateway:** Enforce API key authentication for every single request. Add rate limiting to prevent abuse and filter access by IP address.
*   👤 **Centralized User Management:** Create different "users" for your different apps or family members, each with their own unique, revocable API keys.
*   🌐 **Multi-Server Federation:** Have Ollama running on a few different machines? The proxy unifies them! See and use models from all your servers as if they were one.
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
Open your terminal, navigate to the project folder, and run:
```bash
chmod +x run.sh
./run.sh
```

![GIF placeholder: The setup wizard in action on Windows or macOS, showing the user answering the prompts.](https://your-gif-hosting.com/setup_wizard.gif)

**That's it!** The server is now running. To stop it, just close the terminal window or press `Ctrl+C`.

---

## A Visual Walkthrough

### Step 1: Log In to Your Fortress

Once the server is running, open your web browser and go to the admin panel (e.g., `http://127.0.0.1:8080/admin`). Log in with the admin credentials you created during setup.

![Screenshot: The beautiful and clean Admin Login page.](https://your-image-hosting.com/login_page.png)

### Step 2: Manage Your Users

From the dashboard, you can create new users. These aren't system users; they are virtual users within the proxy, perfect for organizing access.

![Screenshot: The main Admin Dashboard showing a list of created users.](https://your-image-hosting.com/dashboard.png)

### Step 3: Create & Revoke API Keys

Click "Manage" on any user to create unique API keys for them. Give a key a name (e.g., "My Chatbot App"), and a new, secure key will be generated. **Copy this key—it will only be shown once!** If a key is ever compromised, you can revoke it with a single click.

![Screenshot: The User Details page, highlighting the "Create Key" form and the list of active/revoked keys.](https://your-image-hosting.com/user_details.png)

### Step 4: Make Secure API Calls

Now, instead of accessing Ollama directly, you access the proxy. Configure your applications to use the proxy URL and provide the API key as a Bearer token in the `Authorization` header.

```bash
curl http://127.0.0.1:8080/api/generate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer op_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxx" \
  -d '{
    "model": "llama3",
    "prompt": "Why is the sky blue?"
  }'
```

Your request is now authenticated, rate-limited, and logged. Your Ollama instance is completely shielded from the internet.

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

## License

This project is licensed under the Apache License 2.0. Feel free to use, modify, and distribute.
