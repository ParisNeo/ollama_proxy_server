import sys
import secrets
import getpass

def print_info(message):
    print(f"[INFO] {message}")

def print_error(message):
    print(f"[ERROR] {message}", file=sys.stderr)

def get_user_input(prompt, default=None):
    """Gets user input with a default value."""
    default_text = f"[{default}]" if default is not None else ""
    prompt_text = f"   -> {prompt} {default_text}: "
    user_value = input(prompt_text)
    return user_value.strip() if user_value.strip() else default

def get_secure_input(prompt):
    """Gets user input without showing it on the screen."""
    prompt_text = f"   -> {prompt}: "
    return getpass.getpass(prompt_text).strip()

def create_env_file():
    """
    Guides the user through creating a .env file and writes it.
    This version omits empty optional fields to prevent parsing errors.
    """
    print_info("Please provide the following configuration (press Enter for defaults):")

    config = {
        "PROXY_PORT": get_user_input("Port for the proxy server", "8080"),
        "OLLAMA_SERVERS": get_user_input("Backend Ollama server(s)", "http://127.0.0.1:11434"),
        "REDIS_HOST": get_user_input("Redis Host", "localhost"),
        "REDIS_PORT": get_user_input("Redis Port", "6379"),
        "REDIS_USERNAME": get_user_input("Redis Username (optional)", ""),
        "ADMIN_USER": get_user_input("Username for the admin dashboard", "admin"),
        "ALLOWED_IPS": get_user_input("Allowed IPs (comma-separated, leave empty for all)", ""),
        "DENIED_IPS": get_user_input("Denied IPs (comma-separated, leave empty for none)", ""),
    }

    # --- Secure Password Inputs ---
    config["REDIS_PASSWORD"] = get_secure_input("Redis Password (optional, will be hidden)")
    
    admin_password = ""
    while not admin_password:
        admin_password = get_secure_input("Password for the admin user (cannot be empty, will be hidden)")
        if not admin_password:
            print_error("   Admin password cannot be empty. Please try again.")
    config["ADMIN_PASSWORD"] = admin_password

    # --- Generate Secret Key ---
    config["SECRET_KEY"] = secrets.token_hex(32)

    # --- Write the .env file ---
    print_info("Generating .env configuration file...")
    try:
        with open(".env", "w", encoding="utf-8") as f:
            f.write('APP_NAME="Ollama Proxy Server"\n')
            f.write('APP_VERSION="9.0.0"\n')
            f.write('LOG_LEVEL="info"\n')
            f.write(f'PROXY_PORT="{config["PROXY_PORT"]}"\n')
            f.write(f'OLLAMA_SERVERS="{config["OLLAMA_SERVERS"]}"\n')
            f.write('DATABASE_URL="sqlite+aiosqlite:///./ollama_proxy.db"\n')
            f.write(f'ADMIN_USER="{config["ADMIN_USER"]}"\n')
            f.write(f'ADMIN_PASSWORD="{config["ADMIN_PASSWORD"]}"\n')
            f.write(f'SECRET_KEY="{config["SECRET_KEY"]}"\n')
            
            # Write new Redis variables
            f.write(f'REDIS_HOST="{config["REDIS_HOST"]}"\n')
            f.write(f'REDIS_PORT="{config["REDIS_PORT"]}"\n')
            if config["REDIS_USERNAME"]:
                f.write(f'REDIS_USERNAME="{config["REDIS_USERNAME"]}"\n')
            if config["REDIS_PASSWORD"]:
                f.write(f'REDIS_PASSWORD="{config["REDIS_PASSWORD"]}"\n')

            f.write('RATE_LIMIT_REQUESTS="100"\n')
            f.write('RATE_LIMIT_WINDOW_MINUTES="1"\n')
            
            if config["ALLOWED_IPS"]:
                f.write(f'ALLOWED_IPS="{config["ALLOWED_IPS"]}"\n')
            if config["DENIED_IPS"]:
                f.write(f'DENIED_IPS="{config["DENIED_IPS"]}"\n')

        print_info(".env file created successfully.")
        return True
    except IOError as e:
        print_error(f"Failed to write to .env file: {e}")
        return False

if __name__ == "__main__":
    if not create_env_file():
        # Exit with a non-zero code to signal failure to the batch script
        sys.exit(1)
